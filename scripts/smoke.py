"""Exercise the provider directly, no Inspect task and no model.

    .venv/bin/python sandboxes/inspect_openshell/scripts/smoke.py                 # defaults
    .venv/bin/python sandboxes/inspect_openshell/scripts/smoke.py path/to/openshell.yaml

With a config path the run uses that task's image, build step and workdir, so
it checks exactly what an eval would get. It also prints the cues an agent
could use to tell it is in a sandbox: HOME, OPENSHELL_* variables, and any
sandbox-looking names at the filesystem root.
"""

import asyncio
import sys
import time

from inspect_openshell._sandbox import OpenShellSandboxEnvironment


async def main(cfg_path: str | None) -> int:
    from inspect_openshell._sandbox import OpenShellConfig
    cfg = OpenShellConfig.load(cfg_path)
    print(f"config={cfg_path or '(defaults)'} image={cfg.image} workdir={cfg.workdir} build={cfg.build or '-'}")
    t = time.time()
    await OpenShellSandboxEnvironment.task_init("smoke", cfg_path)
    envs = await OpenShellSandboxEnvironment.sample_init("smoke", cfg_path, {})
    sb = envs["default"]
    print(f"created {sb.name} in {time.time()-t:.1f}s")
    ok = True
    try:
        await sb.write_file("hello.py", "print('hi from openshell')\n")
        r = await sb.exec(["python3", "hello.py"])
        print(f"exec python3 hello.py -> rc={r.returncode} stdout={r.stdout.strip()!r} stderr={r.stderr.strip()!r}")
        ok &= r.success and r.stdout.strip() == "hi from openshell"
        r = await sb.exec(["pwd"])
        print(f"exec pwd -> {r.stdout.strip()!r} (expect {sb.config.workdir})")
        ok &= r.stdout.strip() == sb.config.workdir
        r = await sb.exec(["sh", "-c", "exit 7"])
        print(f"exec 'exit 7' -> rc={r.returncode} success={r.success} (expect 7, False)")
        ok &= r.returncode == 7 and not r.success
        txt = await sb.read_file("hello.py")
        print(f"read_file hello.py -> {txt!r}")
        ok &= txt == "print('hi from openshell')\n"
        raw = await sb.read_file("hello.py", text=False)
        ok &= isinstance(raw, bytes)
        try:
            await sb.read_file("missing.txt")
            print("read_file missing.txt -> no error (expected FileNotFoundError)"); ok = False
        except FileNotFoundError:
            print("read_file missing.txt -> FileNotFoundError (correct)")
        r = await sb.exec(["cat"], input="piped\n")
        print(f"exec cat with stdin -> {r.stdout!r}")
        ok &= r.stdout == "piped\n"
        # policy probe: if the config names a policy, show what it refuses
        if cfg.policy:
            for probe in (
                "echo x >> /spec/test_discount.py",
                "rm -f /spec/test_discount.py",
                "mv /spec/test_discount.py /tmp/t.py",
                "cat /spec/test_discount.py > /dev/null && echo read-ok",
                "echo ok > /space/w.txt && echo write-space-ok",
                "python3 -c 'import urllib.request;urllib.request.urlopen(\"http://example.com\",timeout=3)'",
            ):
                r = await sb.exec(["sh", "-c", probe])
                verdict = (r.stdout.strip() or r.stderr.strip().splitlines()[-1] if (r.stdout.strip() or r.stderr.strip()) else "")
                print(f"policy: {probe[:52]:52} -> rc={r.returncode} {verdict[:70]}")
        # word audit: every place the string "sandbox" could reach an agent that looks
        audit = (
            "u=$(id -un); echo user=$u; "
            "echo hostname=$(hostname 2>/dev/null || cat /etc/hostname); "
            "echo passwd_hits=$(grep -c -i sandbox /etc/passwd /etc/group 2>/dev/null | tr '\\n' ' '); "
            "echo env_hits=$(env | grep -i -c sandbox); "
            "echo mounts_hits=$(grep -i -c sandbox /proc/mounts 2>/dev/null); "
            "echo pid1=$(tr '\\0' ' ' < /proc/1/cmdline | cut -c1-80); "
            "echo root_dirs=$(ls / | tr '\\n' ' '); "
            "echo openshell_dirs=$(ls -d /run/openshell* /opt/openshell* /etc/openshell* 2>/dev/null | tr '\\n' ' '); "
            "echo file_hits=$(grep -rl -i sandbox /run/openshell* /opt/openshell* /etc/openshell* /etc 2>/dev/null | head -5 | tr '\\n' ' ')"
        )
        r = await sb.exec(["sh", "-c", audit])
        print("audit for the word 'sandbox' inside the environment:\n     " + r.stdout.strip().replace("\n", "\n     "))
        # informational: what could tell an agent it is in a sandbox?
        r = await sb.exec(["sh", "-c", "id -u; echo HOME=$HOME; echo env-names: $(env | grep -i -E 'sandbox|openshell' | cut -d= -f1 | tr '\\n' ' '); echo root: $(ls / | tr '\\n' ' ')"])
        print("info: uid / HOME / env names / root dirs ->\n     " + r.stdout.strip().replace("\n", "\n     "))
    finally:
        t = time.time()
        await OpenShellSandboxEnvironment.sample_cleanup("smoke", cfg_path, envs, False)
        print(f"deleted {sb.name} in {time.time()-t:.1f}s")
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None)))
