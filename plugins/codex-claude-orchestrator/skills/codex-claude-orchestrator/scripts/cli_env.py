"""Local Claude CLI readiness: executable, advertised flags, login, sandbox, optional paid probe.

``--help`` proves only advertised syntax; the version string is recorded for
diagnosis and never used as an allowlist.  Login state is read locally; only
``verify=True`` makes one small model request.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

PLUGIN_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))
from executable_locator import cli_environment, locate_claude
from process_control import terminate_group

DISPATCH_FLAGS = frozenset({
    "-p", "--model", "--effort", "--output-format", "--verbose", "--json-schema", "--session-id", "--resume",
    "--permission-mode", "--tools", "--allowedTools", "--disallowedTools", "--strict-mcp-config", "--mcp-config",
    "--disable-slash-commands",
})
BUDGET_FLAG = "--max-budget-usd"
SANDBOX = Path("/usr/bin/sandbox-exec")


def flag_advertised(help_text: str, flag: str) -> bool:
    """Match one exact option token; ``-p`` must not match inside ``--print``."""
    return re.search(r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text) is not None


def sandbox_available() -> bool:
    return sys.platform == "darwin" and SANDBOX.is_file()


def check_command(command: list[str], cwd: Path, timeout: float, input_text: str | None = None,
                  env: dict[str, str] | None = None) -> tuple[int | None, str, str, str | None]:
    """Keep diagnostics in memory; callers expose only selected non-secret fields."""
    proc = None
    try:
        proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True, env=env)
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        return proc.returncode, stdout, stderr, None
    except subprocess.TimeoutExpired:
        assert proc is not None
        return None, "", "", "timeout_termination_unconfirmed" if terminate_group(proc) else "timeout"
    except OSError:
        return None, "", "", "execution_error"
    finally:
        if proc is not None:
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()


_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"(?i)(bearer|token|key|secret|password)([\"'=: ]+)[^\s\"',]+"),
    re.compile(r"[^\s@\"']+@[^\s@\"']+\.[A-Za-z]{2,}"),
    re.compile(r"[A-Za-z0-9+/_-]{40,}"),
)


def redact(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda match: (match.group(1) + match.group(2) + "[redacted]") if match.lastindex else "[redacted]",
                            value)
    return value


def excerpt(text: str, limit: int = 400) -> str:
    value = redact(" ".join((text or "").split()))
    return value[:limit] + ("…" if len(value) > limit else "")


def failure_category(text: str) -> str:
    """Classify failures without reflecting provider messages or credential values."""
    lower = text.lower()
    if any(s in lower for s in ("authentication_error", "invalid api key", "invalid_api_key", "token expired",
                                "token has expired", "not logged in", "please run /login")) or re.search(r"\b401\b", lower):
        return "authentication_failed"
    if any(s in lower for s in ("rate_limit", "rate limit", "usage limit", "credit balance", "insufficient credit")) \
            or re.search(r"\b429\b", lower):
        return "quota_or_rate_limited"
    if "permission_denied" in lower or "permission_error" in lower or re.search(r"\b403\b", lower):
        return "access_denied"
    if any(s in lower for s in ("enotfound", "econn", "connection error", "network", "dns", "tls", "ssl")):
        return "network_error"
    return "verification_failed"


def auth_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Never echo arbitrary auth JSON, token sources, organization IDs, or raw errors."""
    safe: dict[str, Any] = {}
    for source, destination in (("authMethod", "auth_method"), ("apiProvider", "provider"),
                                ("subscriptionType", "subscription_type")):
        value = data.get(source)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,39}", value):
            safe[destination] = value
    email = data.get("email")
    if isinstance(email, str) and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        local, domain = email.rsplit("@", 1)
        safe["account_masked"] = local[:1] + "***@" + domain
    return safe


def _probe(code: int | None, out: str, err: str, error: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"exit_code": code}
    if error:
        result["error_kind"] = error
    elif code:
        result["error_kind"] = "nonzero_exit"
    if out:
        result["stdout_excerpt"] = excerpt(out)
    if err:
        result["stderr_excerpt"] = excerpt(err)
    return result


def check(cwd: Path, *, verify: bool = False, model: str = "sonnet", timeout: float = 60) -> dict[str, Any]:
    """Return a readiness report; ``ready`` means local checks passed (and the probe, when requested)."""
    selection = locate_claude()
    path = selection.get("path")
    env = cli_environment(selection)
    report: dict[str, Any] = {
        "checked_at": time.time(), "ready": False, "status": "unchecked", "action": None,
        "cli": {"path": path, "source": selection.get("source"), "installed": bool(path), "version": None,
                "missing_flags": [], "budget_flag": False},
        "auth": {"status": "not_checked", "credential_validity": "not_verified"},
        "sandbox": {"available": sandbox_available(), "used_by": "copy profile"},
        "probe": {"status": "not_requested", "requested_model": model},
    }

    def fail(status: str, action: str) -> dict[str, Any]:
        report.update(status=status, action=action)
        return report

    if not path:
        return fail("cli_not_found", selection.get("action") or "Install Claude Code: https://code.claude.com/docs/en/setup")
    code, out, err, error = check_command([path, "--version"], cwd, min(timeout, 10), env=env)
    if error or code != 0:
        report["cli"]["version_probe"] = _probe(code, out, err, error)
        return fail("cli_unavailable", "Claude CLI could not report its version; check the installation and host execution permissions.")
    version = re.search(r"\b\d+\.\d+\.\d+(?:[-+][\w.]+)?\b", out)
    report["cli"]["version"] = version.group() if version else "unrecognized"
    code, out, err, error = check_command([path, "--help"], cwd, min(timeout, 10), env=env)
    if error or code != 0:
        report["cli"]["help_probe"] = _probe(code, "", err, error)
        return fail("cli_unavailable", "Claude CLI --help failed; check the installation and host permissions.")
    report["cli"]["missing_flags"] = sorted(flag for flag in DISPATCH_FLAGS if not flag_advertised(out, flag))
    report["cli"]["budget_flag"] = flag_advertised(out, BUDGET_FLAG)
    code, out, err, error = check_command([path, "auth", "status", "--json"], cwd, min(timeout, 15), env=env)
    if error:
        report["auth"]["status"] = "check_" + ("timeout" if error.startswith("timeout") else error)
        return fail("auth_check_failed", "Login status could not be read; check keychain/host permissions without clearing credentials.")
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    if not isinstance(data, dict) or type(data.get("loggedIn")) is not bool:
        report["auth"]["status"] = "unknown"
        return fail("auth_check_failed", "claude auth status did not return a recognized JSON status.")
    report["auth"].update(auth_metadata(data), logged_in=data["loggedIn"])
    if not data["loggedIn"]:
        report["auth"]["status"] = "not_logged_in"
        return fail("not_logged_in", "Run `claude auth login` in a terminal (or configure your API/provider authentication), then check again.")
    report["auth"]["status"] = "reported_logged_in"
    if report["cli"]["missing_flags"]:
        return fail("cli_incompatible", "The local Claude CLI does not advertise options dispatch needs: "
                    + ", ".join(report["cli"]["missing_flags"]) + ". Update your Claude CLI, then check again.")
    if verify:
        command = [path, "-p", "--model", model, "--effort", "low", "--output-format", "json", "--tools", "",
                   "--permission-mode", "dontAsk", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                   "--disable-slash-commands", "--no-session-persistence"]
        code, out, err, error = check_command(command, cwd, timeout, "Connectivity check. Reply exactly AUTH_CHECK_OK.", env=env)
        report["probe"]["checked_at"] = time.time()
        if error:
            report["probe"]["status"] = "timeout" if error.startswith("timeout") else error
            return fail("verification_" + report["probe"]["status"], "The online check did not finish; this alone does not mean the login is invalid.")
        try:
            provider = json.loads(out)
        except ValueError:
            provider = None
        if (isinstance(provider, dict) and code == 0 and provider.get("type") == "result"
                and provider.get("subtype") == "success" and not provider.get("is_error")):
            report["probe"]["status"] = "verified"
            report["auth"]["credential_validity"] = "verified_for_request"
            report.update(ready=True, status="verified")
            return report
        category = failure_category(out + "\n" + err)
        report["probe"]["status"] = category
        if category == "authentication_failed":
            report["auth"]["credential_validity"] = "rejected"
        return fail(category, "Check the reported category; network, quota or model access failures do not by themselves mean the login expired.")
    report.update(ready=True, status="local_checks_passed")
    return report
