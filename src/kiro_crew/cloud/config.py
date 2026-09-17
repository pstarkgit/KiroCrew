"""Persisted cloud-launcher config — **profile name only, never credentials**.

Stores the AWS *profile name*, region, and the most-recent instance tag under
``~/.kiro/crew/cloud.json``. AWS credentials are never written here — they are
resolved by the ``aws`` CLI's own provider chain from the profile.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import config_dir

_FILENAME = "cloud.json"
DEFAULT_REGION = "us-east-1"

# Cap at 51 (not 63) to match ec2._TAG_RE / validate_tag: a longer last_tag
# would pass THIS sanitizer but then raise ValidationError on resume (the IAM
# role name kirocrew-ec2-<tag> maxes at 64), defeating the "just treat it as no
# last launch" intent. Keep in lockstep with ec2._TAG_RE.
_TAG_RE = re.compile(r"^[a-zA-Z0-9-]{1,51}$")

#: A digest-pinned image reference, the only form ``cloud/fargate/taskdef.py``
#: accepts. Checked here so a hand-edited ``cloud.json`` naming a movable tag is
#: treated as no Fargate configuration at all, rather than becoming a refusal at
#: the first launch -- which is where the operator has least context for it.


#: The two architectures Fargate runs. Spelled here rather than imported from
#: ``cloud/fargate/taskdef.py`` on purpose: this module is loaded to read a config
#: file and must not pull in the ``cloud`` package's AWS surface to do it. The
#: values are held to the imported set by a test, so the two cannot drift.


#: Upper bounds on how much this reader will retain from one ``fargate`` block. The
#: file is not writable through the agent file-edit tool and is mounted read-only in
#: the sandbox, but a same-UID process outside a sandbox can still write it, so the
#: reader cannot assume the bytes are small. ``load`` catches only ``OSError`` and
#: ``JSONDecodeError``, so an oversized-but-valid-JSON document would otherwise be
#: parsed and every string retained, and the read runs on every request that builds
#: the provisioner list -- an unbounded list or an unbounded string is a gateway
#: memory-exhaustion surface with only manual recovery. A block that exceeds any bound
#: reads as absent, the same as any other malformed block: the ceiling is generous
#: next to any real placement, so a legitimate operator never meets it.
_MAX_LIST_ITEMS = 64
_MAX_STRING_LEN = 2048


#: Ceiling on the whole file, checked BEFORE it is parsed. ``json.loads`` builds its
#: result in memory, so a bound applied to the parsed document is applied too late;
#: the read itself is what must refuse. Generous next to a real ``cloud.json`` of a
#: few hundred bytes, and an over-sized file falls back to defaults exactly as a
#: corrupt one does rather than raising into every cloud command.
_MAX_FILE_BYTES = 1 << 20


@dataclass(frozen=True)
class FargateConfig:
    """Where an operator writes the Fargate lane's placement, image and secrets.

    **Identifiers only, never a secret value.** A crew secret is named by its
    canonical name and its ARN; the value is fetched by the task's execution role
    from Secrets Manager before the container starts, so nothing here is a
    credential and this file's no-secrets contract holds unchanged.

    The engine takes these four fields as a ``FargateLaunchSpec`` and refuses to
    guess any of them -- "an unnamed subnet or security group is the same class of
    error as deleting a task on a guess". This is the place they are written down.
    """

    cluster: str = ""
    subnets: tuple[str, ...] = ()
    security_groups: tuple[str, ...] = ()
    image: str = ""
    #: ``(canonical name, ARN)`` pairs. A pair, not a bare ARN: an ARN alone
    #: cannot say where the secret's NAME ends, because the service appends a
    #: six-character suffix and nothing marks the boundary.
    secrets: tuple[tuple[str, str], ...] = ()
    cpu_architecture: str = "X86_64"
    #: False is the safe direction, and the flag is not the boundary -- a task in
    #: a public subnet with no NAT gateway cannot pull its image without one.
    assign_public_ip: bool = False

    def is_complete(self) -> bool:
        """True when every field the engine requires is present and well-formed.

        INCOMPLETE MEANS ABSENT, and that is the whole design of this method. A
        half-written block must leave the lane unregistered rather than registered
        and refusing: a lane that exists and rejects every launch spends the
        operator's attention at launch time on a mistake that was visible when
        they saved the file.

        The secrets must include one named for the model credential, because the
        engine refuses a task definition that delivers none: a block with every
        placement field and no credential secret is the offered-and-refusing state
        in its most likely form. Only the NAME is judged here. Whether the ARN is
        that name plus one service suffix is the engine's verification, and it is
        not repeated in this module.
        """
        return bool(
            self.cluster
            and self.subnets
            and self.security_groups
            and _digest_pinned(self.image or "")
            and self.cpu_architecture in _cpu_architectures()
            and _names_model_credential(self.secrets)
        )

    @classmethod
    def from_mapping(cls, data: object) -> Optional["FargateConfig"]:
        """Read one block, or ``None`` for anything that is not usable.

        Every rejection returns ``None`` rather than a partially-populated object,
        so a caller cannot hold a config that looks present and is not. A secret
        entry that is not a two-string pair drops the WHOLE block, not just that
        entry: silently launching with one fewer secret than the operator wrote is
        how a task starts and then fails on a missing variable.

        ``assign_public_ip`` is read the same way: absent means ``False``, and a
        present value that is not a JSON boolean drops the block. The field decides
        network exposure, and coercing it would read the string ``"false"`` as
        true, which is the one direction this field must never be guessed in.
        """
        if not isinstance(data, dict):
            return None
        secrets: list[tuple[str, str]] = []
        raw_secrets = data.get("secrets", [])
        if not isinstance(raw_secrets, list) or len(raw_secrets) > _MAX_LIST_ITEMS:
            return None
        for entry in raw_secrets:
            if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
                return None
            name, arn = entry
            if not (isinstance(name, str) and isinstance(arn, str) and name and arn):
                return None
            if len(name) > _MAX_STRING_LEN or len(arn) > _MAX_STRING_LEN:
                return None
            secrets.append((name, arn))
        assign_public_ip = data.get("assign_public_ip", False)
        if not isinstance(assign_public_ip, bool):
            return None
        cluster = str(data.get("cluster", ""))
        image = str(data.get("image", ""))
        cpu_architecture = str(data.get("cpu_architecture", "X86_64"))
        if any(len(field) > _MAX_STRING_LEN for field in (cluster, image, cpu_architecture)):
            return None
        subnets = _bounded_string_tuple(data.get("subnets"))
        security_groups = _bounded_string_tuple(data.get("security_groups"))
        if subnets is None or security_groups is None:
            return None
        candidate = cls(
            cluster=cluster,
            subnets=subnets,
            security_groups=security_groups,
            image=image,
            secrets=tuple(secrets),
            cpu_architecture=cpu_architecture,
            assign_public_ip=assign_public_ip,
        )
        return candidate if candidate.is_complete() else None


def _cpu_architectures() -> frozenset[str]:
    """The architectures the ENGINE accepts, read from it rather than copied.

    Imported on use for the same reason as the credential check below: the value must
    be the engine's, so no copy can drift from it, and the import is effectively free
    once ``cloud/__init__.py`` has run.
    """
    from kiro_crew.cloud.fargate.taskdef import CPU_ARCHITECTURES

    return frozenset(CPU_ARCHITECTURES)


def _digest_pinned(image: str) -> bool:
    """Whether the ENGINE would accept this image reference as digest-pinned.

    Delegates to ``taskdef``'s own refusal so a movable tag is judged here by exactly
    the rule that would reject it at launch -- checked at config time so a hand-edited
    ``cloud.json`` naming a tag reads as no Fargate configuration instead of becoming a
    refusal on first use.
    """
    from kiro_crew.cloud.fargate.taskdef import DocumentRefused, _refuse_undigested_image

    try:
        _refuse_undigested_image(image)
    except DocumentRefused:
        return False
    return True


@dataclass(frozen=True)
class _SecretsOnly:
    """Just enough of a task-definition spec for the engine's destination mapping.

    ``taskdef.secret_destinations`` is annotated for a full ``TaskDefinitionSpec`` but
    reads only ``.secrets``, and the alternative to passing the refs through this shim is
    re-implementing its duplicate-destination rule here -- a second copy of a rule, which
    is the mistake this module has already made three times. Building a real spec instead
    would need an image, an architecture and a validated log spec that this check has no
    business inventing.
    """

    secrets: tuple[Any, ...]


def _names_model_credential(secrets: tuple[tuple[str, str], ...]) -> bool:
    """True when a secret IS the crew's model credential, per the ENGINE's own check.

    Calls ``identity.secret_env_name`` instead of re-deriving what it accepts. Three
    rounds of review found the same defect while this was a local approximation: a
    tail-only test admitted a bare ``KIRO_API_KEY`` and a wrong-prefix
    ``junk/KIRO_API_KEY``, then a truthiness test on the crew segment admitted seven
    more spellings. Each one registered the lane so every launch through it refused --
    the offered-and-refusing state this module exists to prevent, reached from inside
    the check written to prevent it. Delegating makes the two agree by CONSTRUCTION, so
    no spelling can pass here and fail there, and no drift pin is needed because there
    is no second copy to drift.

    The import is local to this path and effectively free: ``identity`` imports only the
    standard library, and ``cloud/__init__.py`` has already loaded ~400 modules by the
    time this module exists, so it adds 5 modules and about 4 ms.
    """
    from kiro_crew.cloud.fargate.identity import SecretRef, sole_binding
    from kiro_crew.cloud.fargate.taskdef import MODEL_CREDENTIAL_ENV, secret_destinations

    if not secrets:
        return False
    refs = tuple(SecretRef(name=name, arn=arn) for name, arn in secrets)
    try:
        # EVERY reference, not just the first that matches. An early return accepted a
        # good credential ref sitting beside a malformed or cross-crew one, and the
        # engine then refused the whole document at launch -- registering a lane that
        # rejects every launch through it, which is the state this module exists to
        # prevent. secret_destinations derives all destinations (and refuses two secrets
        # competing for one), and sole_binding refuses a set naming more than one crew,
        # so both are the engine's own checks rather than a second copy.
        destinations = secret_destinations(_SecretsOnly(refs))
        sole_binding({f"secrets[{i}].valueFrom": ref.arn for i, ref in enumerate(refs)})
    except Exception:  # noqa: BLE001 - any refusal means this set is not usable
        return False
    return MODEL_CREDENTIAL_ENV in destinations


def _bounded_string_tuple(value: object) -> Optional[tuple[str, ...]]:
    """Non-empty strings within the retention bounds, or ``None`` when unusable.

    ``None`` is a positive rejection that voids the whole block, used for the two
    shapes that must not be silently accepted: a list longer than ``_MAX_LIST_ITEMS``
    and a member string longer than ``_MAX_STRING_LEN``. Retaining either without a
    bound is the memory-exhaustion surface ``_MAX_LIST_ITEMS`` exists to close, and
    truncating instead would launch against a placement the operator did not write.

    A non-list still reads as the empty tuple rather than an error, so ``is_complete``
    stays the single place emptiness is judged; an empty required list is what leaves
    the lane unregistered there.
    """
    if not isinstance(value, list):
        return ()
    if len(value) > _MAX_LIST_ITEMS:
        return None
    # ALL-OR-NOTHING. Filtering the bad members out silently launched against a
    # placement the operator did not write: a subnet list with one non-string entry
    # became a shorter list, and the task ran in whichever subnets survived. One bad
    # member voids the block, like one bad secret entry does, so the operator sees an
    # unregistered lane instead of a task in the wrong place.
    for item in value:
        if not isinstance(item, str) or not item or len(item) > _MAX_STRING_LEN:
            return None
    return tuple(value)


@dataclass
class CloudConfig:
    """The launcher's saved state (no secrets)."""

    profile: str = ""
    region: str = DEFAULT_REGION
    last_tag: str = ""
    #: The ``fargate`` block EXACTLY as read from the file, or ``None`` when the
    #: file has none. It is kept raw, not judged, so that ``save()`` writes back
    #: whatever the operator wrote: this object is loaded and re-saved to record
    #: ``last_tag`` after an ordinary EC2 launch, and a field that held only a
    #: judged value would erase a block the operator is half-way through writing.
    #: Whether the block is usable is :meth:`fargate_config`'s question.
    fargate: Any = None

    def fargate_config(self) -> Optional[FargateConfig]:
        """The Fargate block as a typed config, or ``None`` when it is not complete.

        ``None`` is what keeps the lane UNREGISTERED, so an operator who has not
        filled the block in is never offered a lane that would refuse them. This
        judges the raw block on every call rather than once at load, so the file
        round-trips untouched and the seam still sees complete-or-absent.
        """
        return FargateConfig.from_mapping(self.fargate)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "CloudConfig":
        p = path or (config_dir() / _FILENAME)
        try:
            # Size BEFORE parse. The field ceilings below bound what a block may
            # retain, but json.loads builds the whole document in memory first, so a
            # bound applied to the parsed result never runs on the input that would
            # exhaust it. Treated as a corrupt file rather than an error, because
            # every caller of this already tolerates that and nothing here should
            # raise into a cloud command.
            if p.stat().st_size > _MAX_FILE_BYTES:
                return cls()
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        # A hand-edited cloud.json may parse to valid JSON that is NOT an object
        # (e.g. `"hello"`, `[1,2]`, `42`, `null`); the .get() calls below would
        # then raise AttributeError and escape load() (handle_cloud only catches
        # AWS/validation errors), giving a raw traceback on every cloud command.
        # Honor the docstring's tolerate-a-corrupt-file promise: fall back to
        # defaults on any non-object shape.
        if not isinstance(data, dict):
            return cls()
        # Sanitize last_tag at the boundary: a hand-edited/corrupt cloud.json
        # must not carry a malformed tag into the resume path (downstream
        # validate_tag would raise; an empty tag just means "no last launch").
        last_tag = str(data.get("last_tag", ""))
        if last_tag and not _TAG_RE.match(last_tag):
            last_tag = ""
        return cls(
            profile=str(data.get("profile", "")),
            region=str(data.get("region", "") or DEFAULT_REGION),
            last_tag=last_tag,
            # Deliberately NOT sanitized here, unlike last_tag: an incomplete
            # block must survive a save, so it is carried as written and judged
            # by fargate_config() at the point of use.
            fargate=data.get("fargate"),
        )

    def save(self, path: Optional[Path] = None) -> None:
        p = path or (config_dir() / _FILENAME)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(self), indent=2)
        # Never publish a record this module's own reader would then refuse. The raw
        # `fargate` block is carried verbatim so a half-written one survives, and
        # `indent=2` re-expands whatever spacing it arrived with, so a hand-minified
        # block that fits under the read ceiling can cross it once re-serialized. The
        # next `load()` would see an over-size file, fall back to defaults, and the
        # operator's profile, region and Fargate block would be gone with nothing
        # said. Refusing leaves the previous file intact, which is the recoverable
        # direction: the caller keeps a config it can still read.
        if len(payload.encode("utf-8")) > _MAX_FILE_BYTES:
            raise ValueError(
                f"refusing to write {p}: the record serializes to "
                f"{len(payload.encode('utf-8'))} bytes, past the {_MAX_FILE_BYTES}-byte "
                "ceiling load() enforces, so saving it would make this config unreadable"
            )
        # Unique temp name per writer: concurrent cloud invocations must not
        # race on a shared .tmp path (see atomic_write's rationale).
        atomic_write(p, payload)
