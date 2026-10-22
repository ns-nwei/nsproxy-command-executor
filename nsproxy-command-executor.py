#!/usr/bin/env python3
"""Run commands on POP hosts via pteredactyl.

Subcommands:
    gen-host-list  Generate per-POP host lists with `tsh ls` (<service>/<pop>.txt).
    execute        Generate host lists from config, then run the configured
                   command with `pteredactyl exec`, pausing for human
                   confirmation between POPs unless slient is true.

Usage:
    nsproxy-command-executor.py gen-host-list --service nsproxy --pops phx1,tpe1
    nsproxy-command-executor.py execute [--config plan.yaml]
"""

import argparse
import html
import json
import os
import re
import shlex
import sys
from datetime import datetime
from pathlib import Path

import subprocess
import yaml

DEFAULT_CONFIG = "plan.yaml"

# Host name prefixes per service, mirroring docs/run-command-on-host/README.md
SERVICE_PREFIXES = {
    "nsproxy": ("dppool", "nsproxy"),
    "edge": ("nsedgeproxy",),
}

TELEPORT_PROXY = "teleport.netskope.io"
# China POPs live on a separate Teleport proxy
CN_POP_PROXIES = {
    "pek1": "teleport.cnnetskope.io",
    "pvg1": "teleport.cnnetskope.io",
    "pvg2": "teleport.cnnetskope.io",
    "szx1": "teleport.cnnetskope.io",
}
PTEREDACTYL_CONCURRENCY = 4

# Blacklist of dangerous commands, checked against the configured command
# before any host is touched. (regex, human-readable reason); case-insensitive.
DANGEROUS_PATTERNS = [
    (r"\brm\b\s+(?:-{1,2}[\w-]+\s+)*(?:-\w*[rR][\w-]*|--recursive)", "recursive rm (rm -r / rm -rf / rm --recursive)"),
    (r"\bmkfs(\.\w+)?\b", "mkfs (format a filesystem)"),
    (r"\bdd\b[^|;&\n]*\bof=/dev/", "dd writing to a block device"),
    (r">\s*/dev/(?:sd|hd|vd|nvme|xvd)", "redirect into a block device"),
    (r"\b(?:shutdown|reboot|halt|poweroff)\b", "shutdown/reboot the host"),
    (r"\binit\s+[06]\b", "init 0/6 (halt/reboot)"),
    (r":\s*\(\s*\)\s*\{.*\}\s*;", "fork bomb"),
    (r"\bfind\s+/[^|;&\n]*-delete\b", "find / ... -delete"),
    (r"\bchmod\b[^|;&\n]*-[a-zA-Z]*R[a-zA-Z]*\b[^|;&\n]*777\b", "chmod -R 777"),
    (r"\b(?:curl|wget)\b[^|;&\n]*\|\s*(?:ba|z)?sh\b", "pipe curl/wget straight into a shell"),
    (r"\b(?:ip6?tables)\s+(?:-F|--flush)\b", "iptables flush"),
]

# ANSI styling; disabled when stdout is not a TTY or NO_COLOR is set
_COLOR_ENABLED = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _paint(code, text):
    text = str(text)
    return f"\033[{code}m{text}\033[0m" if _COLOR_ENABLED else text


def bold(t): return _paint("1", t)
def cyan(t): return _paint("36", t)
def green(t): return _paint("32", t)
def yellow(t): return _paint("33", t)
def red(t): return _paint("31", t)
def dim(t): return _paint("2", t)
def magenta(t): return _paint("35", t)


DEBUG = False


def debug(msg):
    """Log a debug line to stderr when --debug is set."""
    if DEBUG:
        print(f"{dim('[debug]')} {msg}", file=sys.stderr)


def debug_cmd(cmd):
    """Log the exact external command about to be invoked."""
    debug(f"running: {shlex.join(cmd)}")


def print_plan(config_path, services, pops, commands):
    """Pretty-print the execution plan in a box."""
    width = 44
    print(cyan(f"┌{'─' * width}┐"))

    def row(text, paint=None):
        padded = f" {text}".ljust(width)
        print(f"{cyan('│')}{paint(padded) if paint else padded}{cyan('│')}")

    row("Execution plan", bold)
    row(f"Config:  {config_path}", dim)
    row(f"Command: {commands or '(host lists only)'}", magenta)
    for service in services:
        row(f"  • {service}: {', '.join(pops)}")
    print(cyan(f"└{'─' * width}┘"))


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def generate_host_list(service, pop, out_dir):
    """Run `tsh ls` for a POP and write sorted host names to <service>/<pop>.txt."""
    prefixes = SERVICE_PREFIXES[service]
    if pop in CN_POP_PROXIES:
        proxy = CN_POP_PROXIES[pop]
        # All China POPs share the china-prod cluster; filter hosts by POP name
        cluster = "china-prod"
    else:
        proxy = TELEPORT_PROXY
        cluster = pop
    cmd = ["tsh", "ls", "--proxy", proxy, "-c", cluster]
    debug_cmd(cmd)
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, check=True,
    )
    debug(f"tsh ls returned {len(result.stdout.splitlines())} lines")
    hosts = sorted(
        line.split()[0]
        for line in result.stdout.splitlines()
        if line.split() and line.split()[0].startswith(prefixes)
        and (pop not in CN_POP_PROXIES or f".{pop}." in line.split()[0])
    )
    pop_dir = Path(out_dir) / service
    pop_dir.mkdir(parents=True, exist_ok=True)
    host_file = pop_dir / f"{pop}.txt"
    host_file.write_text("\n".join(hosts) + "\n")
    return host_file, hosts


def ask_yes_no(prompt):
    """Ask a y/n question, repeating until the answer is y or n."""
    while True:
        ans = input(prompt).strip().lower()
        if ans in ("y", "n"):
            return ans == "y"
        print("Please answer y or n.")


def confirm_pop(service, pop, host_file, host_count):
    """Ask the human whether to proceed with this POP. Returns False to abort."""
    return ask_yes_no(
        f"{cyan(f'[{service}/{pop}]')} {host_count} hosts {dim(f'({host_file})')}. Run command? [y/n] "
    )


def cmd_gen_host_list(args):
    if args.service not in SERVICE_PREFIXES:
        sys.exit(f"unknown service '{args.service}', expected one of: {', '.join(SERVICE_PREFIXES)}")
    pops = [p.strip() for p in args.pops.split(",") if p.strip()]
    if not pops:
        sys.exit("--pops must contain at least one POP name")
    for pop in pops:
        host_file, hosts = generate_host_list(args.service, pop, args.out_dir)
        print(f"{cyan(f'[{args.service}/{pop}]')} wrote {green(len(hosts))} hosts -> {dim(host_file)}")


REPORT_CSS = """
:root { color-scheme: light;
  --surface: #fcfcfb; --page: #f9f9f7;
  --ink-1: #0b0b0b; --ink-2: #52514e; --ink-muted: #898781;
  --grid: #e1e0d9; --baseline: #c3c2b7; --border: rgba(11,11,11,0.10);
  --good: #0ca30c; --critical: #d03b3b; --good-text: #006300;
  --series-1: #2a78d6;
}
@media (prefers-color-scheme: dark) { :root {
  --surface: #1a1a19; --page: #0d0d0d;
  --ink-1: #f0efec; --ink-2: #c3c2b7;
  --grid: #2c2c2a; --baseline: #383835; --border: rgba(255,255,255,0.10);
  --good: #0ca30c; --critical: #d03b3b; --good-text: #0ca30c;
  --series-1: #3987e5;
} }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; background: var(--page);
  color: var(--ink-1); font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
.wrap { max-width: 960px; margin: 0 auto; }
h1 { font-size: 18px; margin: 0 0 4px; }
.meta { color: var(--ink-2); margin: 0 0 20px; }
.tiles { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }
.tile { background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 16px; min-width: 140px; }
.tile .label { color: var(--ink-2); font-size: 12px; }
.tile .value { font-size: 28px; font-weight: 600; }
.card { background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px; margin-bottom: 20px; }
.card h2 { font-size: 15px; margin: 0 0 2px; }
.card .cmd-label { color: var(--ink-muted); font-size: 11px; font-weight: 600;
  letter-spacing: 0.08em; text-transform: uppercase; margin: 12px 0 4px; }
.card .cmd { display: block; background: var(--page);
  border: 1px solid var(--grid); border-left: 3px solid var(--series-1);
  border-radius: 6px; padding: 8px 12px;
  color: var(--ink-1); font-family: ui-monospace, monospace;
  font-size: 13px; font-weight: 600; margin: 0 0 14px; word-break: break-all; }
.bar { display: flex; height: 8px; border-radius: 4px; overflow: hidden;
  gap: 2px; margin: 8px 0 4px; background: var(--grid); }
.bar .seg-good { background: var(--good); }
.bar .seg-bad { background: var(--critical); }
.bar-legend { color: var(--ink-2); font-size: 12px; margin-bottom: 12px; }
.bar-legend .ok::before { content: "\\2713\\00a0"; color: var(--good); }
.bar-legend .fail::before { content: "\\2717\\00a0"; color: var(--critical); }
table { border-collapse: collapse; width: 100%; }
th { text-align: left; color: var(--ink-muted); font-weight: 500; font-size: 12px;
  border-bottom: 1px solid var(--baseline); padding: 6px 8px; }
td { padding: 6px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
td.num { font-variant-numeric: tabular-nums; }
.st-ok { color: var(--good-text); font-weight: 600; }
.st-ok::before { content: "\\2713\\00a0"; }
.st-fail { color: var(--critical); font-weight: 600; }
.st-fail::before { content: "\\2717\\00a0"; }
details.card { background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 16px; margin-bottom: 20px; }
details.card summary { list-style: none; display: flex; align-items: baseline;
  gap: 12px; cursor: pointer; }
details.card summary::-webkit-details-marker { display: none; }
details.card summary::before { content: "\\25B8"; color: var(--ink-muted);
  display: inline-block; transition: transform 0.12s; }
details.card[open] summary::before { transform: rotate(90deg); }
details.card summary h2 { display: inline; font-size: 15px; margin: 0; }
details.card summary .card-stat { color: var(--ink-2); font-size: 12px; }
details.card summary .ok::before { content: "\\2713\\00a0"; color: var(--good); }
details.card summary .fail::before { content: "\\2717\\00a0"; color: var(--critical); }
details.card .card-body { margin-top: 8px; }
details pre { background: var(--page); border: 1px solid var(--grid); border-radius: 6px;
  padding: 8px 10px; margin: 6px 0 0; white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, monospace; font-size: 12px; max-height: 400px; overflow: auto; }
.st-ok { color: var(--good-text); font-weight: 600; }
"""


def parse_text_report(path):
    """Parse a pteredactyl text report back into the same structure as its JSON output.

    Text mode keeps the progress bar working (JSON mode disables it upstream),
    so we run text and re-parse. Host blocks are located by "HOST: " headers.
    """
    text = Path(path).read_text()
    summary = {
        "executed_command": None, "total_hosts": 0, "successful": 0,
        "failed": 0, "duration_seconds": 0.0, "start_time": None,
    }
    for key, pat, cast in [
        ("executed_command", r"Executed Command: (.*)", str),
        ("total_hosts", r"Total Hosts:\s+(\d+)", int),
        ("successful", r"Successful:\s+(\d+)", int),
        ("failed", r"Failed:\s+(\d+)", int),
        ("duration_seconds", r"Duration:\s+([\d.]+)s", float),
        ("start_time", r"Start Time:\s+(.*)", str),
    ]:
        m = re.search(pat, text)
        if m:
            summary[key] = cast(m.group(1).strip())

    results = []
    # Host blocks start at "HOST: <name>" and run until the next one
    host_starts = [i for i, ln in enumerate(text.splitlines()) if ln.startswith("HOST: ")]
    lines = text.splitlines()
    for n, i in enumerate(host_starts):
        block = lines[i + 1: host_starts[n + 1]] if n + 1 < len(host_starts) else lines[i + 1:]
        error = None
        output_lines = []
        in_output = False
        for ln in block:
            if not in_output and ln.startswith("Error: "):
                error = ln[len("Error: "):]
            elif ln == "Output:":
                in_output = True
            elif in_output:
                output_lines.append(ln)
        # Trim the leading/trailing blank separators around the output body
        while output_lines and not output_lines[0].strip():
            output_lines.pop(0)
        while output_lines and not output_lines[-1].strip():
            output_lines.pop()
        results.append({
            "hostname": lines[i][len("HOST: "):],
            "output": "\n".join(output_lines),
            "error": error,
            "success": error is None,
        })
    return {"summary": summary, "results": results}


def build_report(run_dir):
    """Aggregate all <service>-<pop>.json files in run_dir into report.html."""
    esc = html.escape
    result_files = sorted(run_dir.glob("*/*.json"))
    if not result_files:
        return None

    total_hosts = total_ok = total_fail = 0
    cards = []
    for rf in result_files:
        with open(rf) as f:
            data = json.load(f)
        summary = data.get("summary", {})
        results = data.get("results") or []
        ok = sum(1 for r in results if r.get("success"))
        fail = len(results) - ok
        total_hosts += len(results)
        total_ok += ok
        total_fail += fail

        pct_ok = (ok / len(results) * 100) if results else 0
        rows = []
        for r in results:
            status = ('<span class="st-ok">Success</span>' if r.get("success")
                      else '<span class="st-fail">Failed</span>')
            output = r.get("output") or ""
            error = r.get("error")
            if error:
                output += f"\n[error] {error}"
            output_cell = ""
            if output:
                output_cell = (f"<details><summary>output</summary>"
                               f"<pre>{esc(output)}</pre></details>")
            rows.append(
                f"<tr><td>{esc(r.get('hostname', ''))}</td>"
                f"<td>{status}</td>"
                f"<td class='num'>{esc(str(r.get('exit_code', '')))}</td>"
                f"<td>{output_cell}</td></tr>"
            )

        cards.append(f"""
<details class="card" open>
  <summary><h2>{esc(rf.parent.name)}/{esc(rf.stem)}</h2>
    <span class="card-stat"><span class="ok">{ok} success</span> &nbsp; <span class="fail">{fail} failed</span> &nbsp; {len(results)} hosts</span>
  </summary>
  <div class="card-body">
  <p class="cmd-label">Command</p>
  <code class="cmd">{esc(summary.get('executed_command', ''))}</code>
  <div class="bar">
    <div class="seg-good" style="width:{pct_ok:.1f}%"></div>
    <div class="seg-bad" style="width:{100 - pct_ok:.1f}%"></div>
  </div>
  <p class="bar-legend"><span class="ok">{ok} success</span> &nbsp; <span class="fail">{fail} failed</span> &nbsp; {len(results)} hosts &nbsp; {summary.get('duration_seconds', 0):.2f}s</p>
  <table>
    <tr><th>Host</th><th>Status</th><th>Exit</th><th>Output</th></tr>
    {''.join(rows)}
  </table>
  </div>
</details>""")

    report = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pteredactyl run report — {esc(run_dir.name)}</title>
<style>{REPORT_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>pteredactyl run report</h1>
  <p class="meta">Run: {esc(run_dir.name)} &nbsp;&middot;&nbsp; Generated: {esc(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}</p>
  <div class="tiles">
    <div class="tile"><div class="label">Hosts</div><div class="value">{total_hosts}</div></div>
    <div class="tile"><div class="label">Success</div><div class="value">{total_ok}</div></div>
    <div class="tile"><div class="label">Failed</div><div class="value">{total_fail}</div></div>
  </div>
  {''.join(cards)}
</div>
</body>
</html>
"""
    report_file = run_dir / "report.html"
    report_file.write_text(report)
    return report_file


def check_blacklist(command, extra_patterns=()):
    """Return the list of blacklist reasons the command trips, empty if clean."""
    hits = []
    patterns = [(p, r) for p, r in DANGEROUS_PATTERNS]
    patterns += [(p, f"custom blacklist: {p}") for p in extra_patterns]
    for pat, reason in patterns:
        if re.search(pat, command, re.IGNORECASE):
            hits.append(reason)
    return hits


def cmd_execute(args):
    config = load_config(args.config)
    # Accept both new keys (services/command) and the original ones (service/commands)
    services = config.get("services") or config.get("service") or []
    pops = config.get("pops") or []
    commands = config.get("command") or config.get("commands") or ""
    # Accept the requested 'slient' key (and the conventional 'silent' spelling)
    slient = bool(config.get("slient") or config.get("silent"))
    concurrency = int(config.get("concurrency") or PTEREDACTYL_CONCURRENCY)

    if not services or not pops:
        sys.exit(f"{args.config} must define non-empty 'service' and 'pops' lists")

    # Blacklist check: dangerous commands are rejected outright, nothing runs
    blacklist = config.get("blacklist") or []
    hits = check_blacklist(commands, blacklist)
    if hits:
        print(red("execution rejected by command blacklist:"))
        for hit in hits:
            print(f"  {red('✗')} {hit}")
        print(f"command: {bold(commands)}")
        sys.exit("edit the plan or the blacklist to proceed")

    # Pre-flight confirmation: show the plan before touching any hosts
    if not slient:
        print_plan(args.config, services, pops, commands)
        if not ask_yes_no(cyan("Proceed? [y/n] ")):
            sys.exit(red("aborted by user"))

    # Step 1: generate host lists for every (service, pop)
    host_files = {}
    for service in services:
        for pop in pops:
            host_file, hosts = generate_host_list(service, pop, args.out_dir)
            host_files[(service, pop)] = (host_file, hosts)
            print(f"{cyan(f'[{service}/{pop}]')} wrote {green(len(hosts))} hosts -> {dim(host_file)}")

    if not commands:
        print(yellow("commands is empty; only host lists were generated."))
        return

    # Step 2: exec command per POP, writing JSON output into a timestamped run dir
    run_dir = Path(args.out_dir) / "output" / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    targets = [(s, p) for s in services for p in pops]
    total = len(targets)
    for idx, (service, pop) in enumerate(targets, 1):
        host_file, hosts = host_files[(service, pop)]
        if not hosts:
            print(f"{cyan(f'[{service}/{pop}]')} {yellow('no hosts found, skipping')}")
            continue
        if not slient and not confirm_pop(service, pop, host_file, len(hosts)):
            sys.exit(red(f"aborted at {service}/{pop}"))
        # Prominent "now executing" line so the current service/pop is always visible
        print(f"\n{bold(cyan(f'→ [{idx}/{total}]'))} {bold(f'{service}/{pop}')} "
              f"executing on {green(len(hosts))} hosts ...")
        result_txt = run_dir / service / f"{pop}.txt"
        result_txt.parent.mkdir(parents=True, exist_ok=True)
        # Text mode keeps pteredactyl's progress bar (JSON mode disables it);
        # we parse the text report back into JSON ourselves.
        cmd = ["pteredactyl", "exec", commands, str(host_file),
               "-c", str(concurrency), "-o", str(result_txt)]
        debug_cmd(cmd)
        subprocess.run(
            cmd,
            check=True,
        )
        data = parse_text_report(result_txt)
        result_file = run_dir / service / f"{pop}.json"
        result_file.write_text(json.dumps(data, indent=2))
        if data["results"]:
            result_txt.unlink()
        else:
            print(f"{yellow(f'[{service}/{pop}]')} failed to parse {result_txt}, kept for inspection")
        print(f"{cyan(f'[{service}/{pop}]')} results -> {dim(result_file)}")

    # Step 3: aggregate all result JSONs into an HTML report and open it
    report_file = build_report(run_dir)
    if report_file:
        print(f"{bold('report')} -> {green(report_file)}")
        debug_cmd(["open", str(report_file)])
        subprocess.run(["open", str(report_file)], check=False)


def main():
    global DEBUG
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    # Shared flags available on every subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--debug", action="store_true",
                        help="Log external commands being invoked (tsh, pteredactyl)")

    p_gen = sub.add_parser("gen-host-list", parents=[common],
                           help="Generate host lists only (no command execution)")
    p_gen.add_argument("--service", required=True, help="Service name: nsproxy or edge")
    p_gen.add_argument("--pops", required=True, help="Comma-separated POP names, e.g. phx1,tpe1")
    p_gen.add_argument("--out-dir", default=".", help="Directory to place <service>/<pop>.txt files")
    p_gen.set_defaults(func=cmd_gen_host_list)

    p_exec = sub.add_parser("execute", parents=[common],
                            help="Generate host lists from config and run the configured command")
    p_exec.add_argument("--config", default=DEFAULT_CONFIG, help=f"Path to config file (default: {DEFAULT_CONFIG})")
    p_exec.add_argument("--out-dir", default=".", help="Directory to place <service>/<pop>.txt files")
    p_exec.set_defaults(func=cmd_execute)

    args = parser.parse_args()
    DEBUG = args.debug
    args.func(args)


if __name__ == "__main__":
    main()
