from brain.tools import ToolEngine


def test_interruption_invalidates_critical_readback_without_retaining_factor():
    engine = ToolEngine(None, None, None, None)
    state = {
        "readback": {
            "kind": "patient",
            "revision": 2,
            "say": {"en": "Secret patient", "ur": "مریض"},
        },
        "history": [{"role": "assistant", "content": "full undelivered response"}],
    }
    engine.trusted_event(state, "delivery_interrupted", delivered_text="partial")
    assert state["readback"] is None
    assert "undelivered" not in str(state) and "Secret patient" not in str(state)
