"""YAML flows: automation without writing Python.

    # orders.flow.yaml
    name: Export the orders
    vars: {user: Jean}
    steps:
      - wait: text:Connexion
        timeout: 30
      - type: ${user}
        into: "text:Utilisateur : @right:200"
      - type: ${env:APP_PASSWORD}
        into: "text:Mot de passe : @right:200"
        secret: true
      - click: text:Valider
        expect: text:Bienvenue
      - if_visible: exact:Message du jour
        then:
          - click: exact:Fermer
      - read_table: exact:CMD-0001[1]
        columns: [Numéro, Client, Montant, Statut]
        save_as: orders
        csv: results/orders.csv
      - assert_text: {in: "text:Total : @right:120", matches: '\\d+'}
    recover:                       # run when a step fails, before the next test
      - press: esc
    # several tests in one file:  tests: {name: [steps], other: [steps]}

Every step maps to one Session method; the same engine runs the `recovery` steps of a
profile. `${var}` and `${env:NAME}` are replaced in every string.
"""
from __future__ import annotations

import csv
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import SightlineError
from .geometry import Rect
from .spec import parse_locator

if TYPE_CHECKING:
    from .session import Session

_VAR = re.compile(r"\$\{([^}]+)\}")


@dataclass
class Flow:
    name: str
    tests: dict[str, list[dict]]
    recover: list[dict] = field(default_factory=list)
    vars: dict[str, Any] = field(default_factory=dict)
    profile: str | None = None
    path: str | None = None


def load_flow(path: str | os.PathLike) -> Flow:
    try:
        import yaml
    except ImportError as exc:
        raise SightlineError("YAML flows need: pip install 'sightline[flows]'") from exc
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SightlineError(f"{p}: a flow is a mapping with 'steps:' or 'tests:'")
    if "tests" in data:
        tests = {str(k): v for k, v in data["tests"].items()}
    elif "steps" in data:
        tests = {data.get("name") or p.name.split(".")[0]: data["steps"]}
    else:
        raise SightlineError(f"{p}: needs 'steps:' (one test) or 'tests:' (several)")
    for name, steps in tests.items():
        if not isinstance(steps, list):
            raise SightlineError(f"{p}: test '{name}' must be a list of steps")
    return Flow(data.get("name") or p.stem, tests, data.get("recover") or [],
                data.get("vars") or {}, data.get("profile"), str(p))


def interpolate(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        def sub(m):
            key = m.group(1)
            if key.startswith("env:"):
                name = key[4:]
                if name not in os.environ:
                    raise SightlineError(f"environment variable {name} is not set")
                return os.environ[name]
            if key not in variables:
                raise SightlineError(f"unknown variable ${{{key}}} (have: {sorted(variables)})")
            return str(variables[key])
        if _VAR.fullmatch(value) and not value.startswith("${env:") and value[2:-1] in variables:
            return variables[value[2:-1]]          # keep non-string values (tables...)
        return _VAR.sub(sub, value)
    if isinstance(value, list):
        return [interpolate(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v, variables) for k, v in value.items()}
    return value


def _loc(v):
    return parse_locator(str(v)) if v is not None else None


def _opt(step: dict, key: str, cast=None, default=None):
    v = step.get(key, default)
    return cast(v) if (cast and v is not None) else v


ACTIONS = ("wait", "wait_gone", "click", "double_click", "right_click", "hover", "type", "press",
           "scroll", "scroll_until_visible", "assert_visible", "assert_not_visible",
           "assert_text", "read_text", "read_table", "handle_dialogs", "menu", "select",
           "wait_stable", "screenshot", "sleep", "if_visible", "repeat", "set", "log", "run",
           "drag")


def run_step(s: Session, step: dict, variables: dict[str, Any], base_dir: str = ".") -> Any:
    if not isinstance(step, dict):
        raise SightlineError(f"a step is a mapping like {{click: text:OK}}, got {step!r}")
    action = next((k for k in step if k in ACTIONS), None)
    if action is None:
        raise SightlineError(f"unknown step {step!r}; actions: {', '.join(ACTIONS)}")
    raw = step
    step = dict(step)
    if action not in ("if_visible", "repeat"):     # nested step lists are interpolated later
        step = interpolate(step, variables)
    arg = step[action]
    timeout = _opt(step, "timeout", float)

    if action == "wait":
        return s.wait(_loc(arg), timeout)
    if action == "wait_gone":
        return s.wait_vanish(_loc(arg), timeout)
    if action in ("click", "double_click", "right_click"):
        off = step.get("offset") or (0, 0)
        kw = dict(timeout=timeout, offset=(int(off[0]), int(off[1])),
                  expect=_loc(step.get("expect")), vanish=bool(step.get("vanish", False)),
                  retries=int(step.get("retries", 0)),
                  expect_timeout=_opt(step, "expect_timeout", float))
        if step.get("optional") and s.exists(_loc(arg), timeout or 0) is None:
            return None
        return getattr(s, action)(_loc(arg), **kw)
    if action == "hover":
        return s.hover(_loc(arg), timeout=timeout)
    if action == "drag":
        return s.drag(_loc(arg), _loc(step["to"]), timeout=timeout)
    if action == "type":
        return s.type(str(arg), into=_loc(step.get("into")), clear=step.get("clear", False),
                      secret=bool(step.get("secret", False)))
    if action == "press":
        return s.press(*(arg if isinstance(arg, list) else [arg]))
    if action == "scroll":
        return s.scroll(_loc(step.get("at")), int(arg), bool(step.get("horizontal", False)))
    if action == "scroll_until_visible":
        return s.scroll_until_visible(_loc(arg), _loc(step.get("area")),
                                      direction=step.get("direction", "down"))
    if action == "assert_visible":
        return s.wait(_loc(arg), timeout)
    if action == "assert_not_visible":
        m = s.exists(_loc(arg), timeout or 0)
        if m is not None:
            raise SightlineError(f"assert_not_visible: {arg} is visible ({m})",
                                 s.tracer.failure("assert_not_visible", str(arg), str(m),
                                                  s.capture(), [m.rect]))
        return None
    if action in ("assert_text", "read_text"):
        where = arg["in"] if isinstance(arg, dict) else arg
        text = s.read_text(_loc(where), timeout)
        if action == "read_text":
            if step.get("save_as"):
                variables[step["save_as"]] = text
            return text
        spec = arg if isinstance(arg, dict) else step
        ok, how = True, ""
        if "equals" in spec:
            ok, how = text.strip() == str(spec["equals"]).strip(), f"== {spec['equals']!r}"
        if ok and "contains" in spec:
            ok, how = str(spec["contains"]) in text, f"contains {spec['contains']!r}"
        if ok and "matches" in spec:
            ok, how = re.search(str(spec["matches"]), text) is not None, f"matches {spec['matches']!r}"
        if not ok:
            raise SightlineError(f"assert_text: read {text!r}, expected it {how}",
                                 s.tracer.failure("assert_text", str(where), text, s.capture()))
        return text
    if action == "read_table":
        from .tables import read_table_via_clipboard
        table = read_table_via_clipboard(s, _loc(arg), columns=step.get("columns"),
                                         must_contain=step.get("must_contain") or (),
                                         quoted=bool(step.get("quoted", False)))
        if step.get("save_as"):
            variables[step["save_as"]] = table
        if step.get("csv"):
            out = Path(base_dir, step["csv"])
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f, delimiter=step.get("delimiter", ";")).writerows(table)
        return table
    if action == "handle_dialogs":
        from .dialogs import handle_dialogs
        spec = arg if isinstance(arg, dict) else {"required": arg}
        as_list = lambda v: [] if not v else (v if isinstance(v, list) else [v])
        return handle_dialogs(s, [_loc(x) for x in as_list(spec.get("required"))],
                              [_loc(x) for x in as_list(spec.get("optional"))],
                              float(spec.get("timeout", 20)), float(spec.get("quiet", 3)))
    if action == "menu":
        from .actions import menu
        return menu(s, arg, timeout=timeout)
    if action == "select":
        from .actions import select
        return select(s, _loc(arg), str(step["option"]), timeout=timeout)
    if action == "wait_stable":
        region = Rect(*arg) if isinstance(arg, list) else None
        return s.wait_until_stable(region, float(step.get("quiet", 0.5)), timeout)
    if action == "screenshot":
        out = Path(s.tracer.dir or base_dir, f"{arg}.png")
        return s.screenshot(str(out))
    if action == "sleep":
        time.sleep(float(arg))
        return None
    if action == "if_visible":
        cond = s.exists(_loc(interpolate(arg, variables)), timeout or 0) is not None
        branch = raw.get("then" if cond else "else") or []
        return run_steps(s, branch, variables, base_dir)
    if action == "repeat":
        spec = raw["repeat"] if isinstance(raw["repeat"], dict) else {"times": raw["repeat"]}
        body = raw.get("steps") or []
        times = int(interpolate(spec.get("times", 1000), variables))
        until = spec.get("until_visible")
        for _ in range(times):
            if until and s.exists(_loc(interpolate(until, variables))) is not None:
                return None
            run_steps(s, body, variables, base_dir)
        if until:
            raise SightlineError(f"repeat: {until} never became visible after {times} rounds")
        return None
    if action == "set":
        variables.update(arg)
        return None
    if action == "log":
        s.tracer.note("log", str(arg))
        print(str(arg))
        return None
    if action == "run":
        sub = load_flow(Path(base_dir, arg))
        for steps in sub.tests.values():
            run_steps(s, steps, {**sub.vars, **variables}, str(Path(sub.path).parent))
        return None
    raise SightlineError(f"step {action!r} not implemented")          # pragma: no cover


def run_steps(s: Session, steps: list[dict], variables: dict[str, Any] | None = None,
              base_dir: str = ".") -> dict[str, Any]:
    variables = variables if variables is not None else {}
    for i, step in enumerate(steps, 1):
        try:
            run_step(s, step, variables, base_dir)
        except SightlineError as e:
            lines = str(e).splitlines()
            err = SightlineError(f"step {i} {_short(step)}: " + "\n".join(lines))
            err.artifact = e.artifact                      # keep the debug screenshot
            raise err from e
    return variables


def _short(step) -> str:
    try:
        k = next(iter(step))
        return f"[{k}: {step[k]}]"
    except Exception:
        return repr(step)[:60]
