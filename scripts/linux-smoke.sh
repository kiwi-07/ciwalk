#!/usr/bin/env bash
# Linux smoke: tests 1–4 from the release checklist.
# Designed for ubuntu-latest (native Docker) — also runs on Mac, but that is NOT a Linux host check.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CIWALK="${CIWALK:-$ROOT/.venv/bin/ciwalk}"
if [[ ! -x "$CIWALK" ]]; then
  CIWALK="$(command -v ciwalk)"
fi

QA="$(mktemp -d /tmp/ciwalk-linux-smoke.XXXXXX)"
trap 'rm -rf "$QA"' EXIT
cd "$QA"

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; exit 1; }

cat > test1-basic.yml <<'EOF'
name: basic
jobs:
  build:
    steps:
      - name: hello
        run: echo "hello from ciwalk" && whoami && pwd
EOF

cat > test2-mount.yml <<'EOF'
name: mount-check
jobs:
  build:
    steps:
      - name: list-repo-files
        run: ls -la
EOF

cat > test3-sequence.yml <<'EOF'
name: sequence
jobs:
  build:
    steps:
      - name: write-file
        run: echo "step1-was-here" > /tmp/marker.txt
      - name: read-file
        run: cat /tmp/marker.txt
EOF

cat > test4-fail.yml <<'EOF'
name: fail-test
jobs:
  build:
    steps:
      - name: setup
        run: echo "setting up"
      - name: broken-step
        run: cat /github/workspace/does-not-exist.txt
      - name: after
        run: echo "should only print after you fix it"
EOF

echo "=== host $(uname -s)/$(uname -m) docker=$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null || echo '?') ==="
echo "=== ciwalk: $CIWALK ==="

echo ""
echo "========== T1 basic =========="
out="$("$CIWALK" run test1-basic.yml 2>&1)" || true
printf '%s\n' "$out"
echo "$out" | grep -q 'hello from ciwalk' || fail "T1 missing hello"
echo "$out" | grep -q '/github/workspace' || fail "T1 missing workspace path"
echo "$out" | grep -q 'Job passed' || fail "T1 not passed"
cid="$(echo "$out" | sed -n 's/.*container:[[:space:]]*\([a-f0-9]*\).*/\1/p' | head -1)"
if [[ -n "$cid" ]] && docker inspect "$cid" >/dev/null 2>&1; then
  fail "T1 container $cid still present without --keep"
fi
pass "T1 basic orchestration + auto-remove"

echo ""
echo "========== T2 workspace mount =========="
# Run with --workdir pointing at the real project so we see host files
out="$("$CIWALK" run test2-mount.yml --workdir "$ROOT" 2>&1)" || true
printf '%s\n' "$out"
echo "$out" | grep -qE 'pyproject.toml|README.md|src' || fail "T2 host files not visible in mount"
echo "$out" | grep -q 'Job passed' || fail "T2 not passed"
pass "T2 bind-mount shows host project files (uid/gid mapping ok enough to read)"

echo ""
echo "========== T3 multi-step persistence =========="
out="$("$CIWALK" run test3-sequence.yml 2>&1)" || true
printf '%s\n' "$out"
echo "$out" | grep -q 'step1-was-here' || fail "T3 marker not persisted across steps"
echo "$out" | grep -q 'Job passed' || fail "T3 not passed"
pass "T3 same-container state across steps"

echo ""
echo "========== T4 pause-on-fail + fix + retry =========="
rm -f does-not-exist.txt
# Prefer expect when available; else a small Python PTY driver
if command -v expect >/dev/null 2>&1; then
  expect <<EOF
set timeout 120
log_user 1
spawn env TERM=xterm $CIWALK run test4-fail.yml --pause-on-fail
expect {
  -re {dropping into a shell} {}
  timeout { puts "FAIL timeout pause"; exit 2 }
}
expect {
  -re {bash-5} {}
  timeout { puts "FAIL timeout prompt"; exit 3 }
}
send -- "echo '{}' > does-not-exist.txt\r"
expect {
  -re {bash-5} {}
  timeout { puts "FAIL timeout after write"; exit 4 }
}
send -- "exit\r"
expect {
  -re {Action} {}
  timeout { puts "FAIL timeout action"; exit 5 }
}
send -- "r\r"
expect eof
catch wait result
set ec [lindex \$result 3]
puts "EXPECT_EXIT=\$ec"
if {\$ec == 0} { exit 0 } else { exit \$ec }
EOF
  t4_ec=$?
else
  export CIWALK
  python3 - <<'PY'
import os, pty, re, select, subprocess, sys, time
ciwalk = os.environ["CIWALK"]
master, slave = pty.openpty()
env = {**os.environ, "TERM": "xterm"}
proc = subprocess.Popen(
    [ciwalk, "run", "test4-fail.yml", "--pause-on-fail"],
    stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
)
os.close(slave)
buf = b""
phase = 0
deadline = time.time() + 120
while time.time() < deadline and proc.poll() is None:
    r, _, _ = select.select([master], [], [], 0.25)
    if not r:
        continue
    try:
        chunk = os.read(master, 4096)
    except OSError:
        break
    if not chunk:
        break
    buf += chunk
    plain = re.sub(rb"\x1b\[[0-9;]*[A-Za-z]", b"", buf).decode("utf-8", "replace")
    if phase == 0 and "bash-5" in plain:
        time.sleep(0.2)
        os.write(master, b"echo '{}' > does-not-exist.txt\r")
        phase = 1
    elif phase == 1 and plain.rstrip().endswith("#") and "does-not-exist" in plain:
        time.sleep(0.2)
        os.write(master, b"exit\r")
        phase = 2
    elif phase == 2 and "Action" in plain:
        time.sleep(0.2)
        os.write(master, b"r\r")
        phase = 3
        break
try:
    os.close(master)
except OSError:
    pass
ec = proc.wait(timeout=60)
sys.stdout.write(re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", buf.decode("utf-8", "replace")))
raise SystemExit(ec)
PY
  t4_ec=$?
fi

[[ "$t4_ec" -eq 0 ]] || fail "T4 exit=$t4_ec"
# Prefer evidence the job finished green. The host-file check can false-fail under nested
# Docker-in-Docker (/tmp in the runner container ≠ dockerd host /tmp bind source).
if [[ -f does-not-exist.txt ]]; then
  pass "T4 pause-on-fail fix+retry (bind-mount write visible on host)"
else
  echo "NOTE: host file not visible here (often DinD nesting); T4 CLI exit=0 still required"
  pass "T4 pause-on-fail fix+retry (Job passed / exit 0)"
fi

echo ""
echo "All Linux smoke checks (T1–T4) passed on $(uname -s)."
