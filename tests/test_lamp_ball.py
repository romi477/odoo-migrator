"""The lamp of the module in work is a ball bouncing in a ring."""

import shutil
import subprocess
import textwrap

import pytest

from odoo_migrator.paths import static_dir

NODE = shutil.which("node")


def _script():
    return static_dir() / "lamp-ball.js"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_ball_never_leaves_the_ring():
    """Bounces off the wall by the law of reflection, with a little chaos in
    the angle; wherever it goes it stays inside, for any step the browser
    may hand it."""
    program = textwrap.dedent(
        f"""
        const {{ stepBall }} = require({str(_script())!r});
        let seed = 12345;
        const rand = () => (seed = (seed * 1664525 + 1013904223) % 4294967296) / 4294967296;
        const ball = {{ x: 0, y: 0, vx: 24, vy: 5, limit: 6.4 }};
        let worst = 0, farthest = 0, turns = 0, lastAngle = Math.atan2(ball.vy, ball.vx);
        for (let i = 0; i < 200000; i++) {{
          stepBall(ball, rand() * 0.05, rand);
          const d = Math.hypot(ball.x, ball.y);
          worst = Math.max(worst, d - ball.limit);
          farthest = Math.max(farthest, d);
          const angle = Math.atan2(ball.vy, ball.vx);
          if (Math.abs(angle - lastAngle) > 1e-6) turns++;
          lastAngle = angle;
        }}
        console.log(JSON.stringify({{ worst, farthest, turns }}));
        """
    )

    out = subprocess.run(
        [NODE, "-e", program], capture_output=True, text=True, check=True
    ).stdout

    import json

    result = json.loads(out)
    assert result["worst"] <= 1e-9
    # It reaches the wall rather than orbit the middle, and it turns often: a
    # path that bounced twice and settled would be a line, not a ball at play.
    assert result["farthest"] > 6.3
    assert result["turns"] > 1000


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_bounce_turns_the_ball_back_in():
    program = textwrap.dedent(
        f"""
        const {{ stepBall }} = require({str(_script())!r});
        const ball = {{ x: 6.3, y: 0, vx: 24, vy: 0, limit: 6.4 }};
        stepBall(ball, 0.01, () => 0.5);
        console.log(JSON.stringify(ball));
        """
    )

    import json

    ball = json.loads(
        subprocess.run([NODE, "-e", program], capture_output=True, text=True, check=True).stdout
    )

    assert ball["vx"] < 0
    assert ball["x"] <= 6.4 + 1e-9


def test_the_running_lamp_hands_over_to_the_ball_and_the_sonar_is_gone(client):
    css = client.get("/static/style.css").text
    script = client.get("/static/lamp-ball.js").text

    assert ".pipeline-lamp.has-ball" in css
    assert "pipeline-eq-" not in css
    assert "pipeline-sonar" not in css
    # A person who asked the system for less motion gets a still ring.
    assert "prefers-reduced-motion" in script


def test_the_runner_page_loads_the_ball_before_the_queue_script(client):
    from test_batch import _prepared

    _prepared(client)
    page = client.get("/run").text

    assert page.index("/static/lamp-ball.js") < page.index("/static/pipeline.js")
