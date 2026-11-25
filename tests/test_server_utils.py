from argo.server import messages_to_task


def test_messages_to_task_includes_history_and_system():
    task = messages_to_task([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": [{"type": "text", "text": "second"}]},
    ])
    assert "be terse" in task
    assert "USER: first" in task
    assert "second" in task
