import pytest

from odoo_migrator.runner.cartridge import CartridgeError, load_cartridge

MINIMAL = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: agent
    id: port
    sessions: [target]
    tools: [write_file]
    context: [rules]
    prompt: write it
  - step: verdict
on_failure:
  pipeline:
    - step: agent
      id: diagnose
      sessions: [target]
      tools: [exec]
      context: [last_failure]
      raises: [small_fix]
      prompt: why
    - step: agent
      id: patch
      when: small_fix
      sessions: [target]
      tools: [write_file]
      context: [previous_diff]
      prompt: fix it
  retry:
    resume_at: verdict
"""


def test_a_valid_cartridge_loads_and_carries_its_hash():
    loaded = load_cartridge(MINIMAL)

    assert loaded.cartridge == "t"
    assert [step.id or step.kind for step in loaded.pipeline] == ["port", "verdict"]
    assert len(loaded.sha256) == 64


def test_json_is_accepted_too():
    """yaml.safe_load parses JSON, so an uploaded .json file is not a refusal."""
    as_json = load_cartridge(
        '{"cartridge":"t","version":"1.0.0","schema":1,'
        '"model":{"provider":"anthropic","name":"claude-sonnet-5-5"},'
        '"pipeline":[{"step":"verdict"}],"on_failure":{"pipeline":[],'
        '"retry":{"resume_at":"verdict"}}}'
    )

    assert as_json.cartridge == "t"


@pytest.mark.parametrize(
    "broken, says",
    [
        ("unknown_step", "step"),
        ("unknown_flag", "flag"),
        ("unraised_flag", "raise"),
        ("unread_flag", "read"),
        ("unknown_tool", "tool"),
        ("tool_without_session", "session"),
        ("unknown_context", "context"),
        ("duplicate_id", "id"),
        ("agent_without_prompt", "prompt"),
        ("prompt_on_deterministic", "prompt"),
        ("resume_at_nowhere", "resume_at"),
        ("unknown_provider", "provider"),
        ("future_schema", "schema"),
    ],
)
def test_every_refusal_names_what_is_wrong(broken, says, cartridge_text):
    with pytest.raises(CartridgeError) as refused:
        load_cartridge(cartridge_text(broken))

    assert says in str(refused.value).lower()


def test_the_shipped_stock_cartridge_loads():
    from odoo_migrator.paths import repo_dir
    from odoo_migrator.runner.cartridge import load_cartridge_file

    loaded = load_cartridge_file(repo_dir() / "cartridges" / "stock.yaml")

    assert loaded.cartridge == "stock"
    assert loaded.step_by_id("understand") is not None
    assert loaded.on_failure.retry.resume_at == "lint"


def test_understand_is_given_the_rules_it_is_asked_to_predict_with():
    """Its prompt asks what will break on the target version. Without the
    stack it had the module's source and nothing describing the target, so
    the answer the port reads was a guess from memory."""
    from odoo_migrator.paths import repo_dir
    from odoo_migrator.runner.cartridge import load_cartridge_file

    loaded = load_cartridge_file(repo_dir() / "cartridges" / "stock.yaml")

    assert "rules" in loaded.step_by_id("understand").context
