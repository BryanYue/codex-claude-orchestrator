"""Check explicit approved-document identities; never infer human approval."""
import hashlib
from pathlib import Path, PurePosixPath
import re
import subprocess


def validate_binding(packet):
    binding = packet.get("protocol_binding")
    if binding is None:
        return packet
    if not isinstance(binding, dict):
        raise ValueError("protocol_binding must be an object")
    base = binding.get("baseline_commit", "")
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("protocol_binding requires an explicit full approved baseline_commit")
    approval = binding.get("approval_source")
    if not isinstance(approval, str) or not approval.strip():
        raise ValueError("protocol_binding requires an approval_source; tools cannot invent approval")
    cwd = Path(packet["cwd"]).resolve()
    root = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True).stdout.strip()
    if Path(root).resolve() != cwd:
        raise ValueError("protocol_binding cwd must be the project root")
    paths = {}
    for key in ("protocol_path", "delta_path", "dispatch_path", "progress_path"):
        value = binding.get(key)
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise ValueError(f"Invalid protocol_binding {key}")
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(p in {".", ".."} for p in value.split("/")):
            raise ValueError(f"protocol_binding {key} must be a literal relative file")
        path = cwd
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError(f"protocol_binding {key} cannot traverse a symlink")
        if not path.is_file():
            raise ValueError(f"Missing protocol_binding file: {key}")
        paths[key] = path
    state = paths["delta_path"].parent
    if any(paths[key].parent != state for key in ("dispatch_path", "progress_path")):
        raise ValueError("delta, dispatch and PROGRESS must belong to one task directory")
    for prefix in ("protocol", "delta"):
        expected = binding.get(prefix + "_sha256", "")
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"Explicit {prefix}_sha256 is required")
        file = binding[prefix + "_path"]
        baseline = subprocess.run(["git", "-C", str(cwd), "show", f"{base}:{file}"], capture_output=True, check=True).stdout
        if hashlib.sha256(baseline).hexdigest() != expected:
            raise ValueError(f"{prefix} does not match the declared approved baseline/hash")
        if hashlib.sha256(paths[prefix + "_path"].read_bytes()).hexdigest() != expected:
            raise ValueError(f"Working {prefix} differs from the approved identity; reconcile before dispatch")
    # These source identities enter the bridge's existing before/after and resume
    # protections. Dispatch and PROGRESS stay adjustable, owned by the coordinator.
    result = dict(packet)
    result["requirement_sources"] = list(dict.fromkeys(packet["requirement_sources"] +
        [str(paths["protocol_path"]), str(paths["delta_path"])]))
    return result
