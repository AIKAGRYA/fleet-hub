from __future__ import annotations

import subprocess
from pathlib import Path


def test_host_runner_loads_environment_values_literally(tmp_path: Path) -> None:
    payload = "$6$literal value#tail;$(touch should-not-exist)"
    env_file = tmp_path / "hub.env"
    env_file.write_text(
        f"NATS_URL=nats://127.0.0.1:4222\nNATS_PASS={payload}\n",
        encoding="utf-8",
    )

    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "server.py").write_text("app = None\n", encoding="utf-8")

    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "[[ $NATS_URL == nats://127.0.0.1:4222 ]] || exit 90\n"
        f"[[ $NATS_PASS == {payload!r} ]] || exit 91\n",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    runner = Path(__file__).parents[2] / "scripts" / "run_hub_from_env.sh"
    result = subprocess.run(
        [str(runner), str(env_file), str(fake_python), str(app_dir), "8872"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "should-not-exist").exists()
