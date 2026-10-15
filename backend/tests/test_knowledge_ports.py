from knowledge_core.ports import LocalParser, ModelConfig
from knowledge_core.runtime import Runtime
from knowledge_core.identity import LocalIdentity


def test_ports_are_injected_without_host_imports(tmp_path):
    calls = []
    class Model:
        def generate_json(self, config, prompt, payload):
            calls.append((config, prompt, payload))
            return {"buckets": [{"title": "Injected parser", "summary": "a.txt!", "section_ids": ["sec_1"]}]}
    class Parser:
        def parse(self, filename, content):
            return filename + content.decode(), "txt"
    config = ModelConfig("http://model", "selected", "fixture")
    runtime = Runtime(f"sqlite:///{tmp_path / chr(107)}", model=Model(), parser=Parser(), model_config=config)
    runtime.initialize()
    actor = LocalIdentity().authenticate(None)
    model, selected, parser = runtime.ports({})
    assert selected == config
    text, file_type = parser.parse("a.txt", b"!")
    row = runtime.content.write("injected", "a.txt", text, None, {}, {}, actor, file_type=file_type)
    assert row["content"] == "a.txt!" and row["file_type"] == "txt"
    assert calls and all(call[0] == config for call in calls)
    runtime.close()


def test_local_parser_preserves_format():
    assert LocalParser().parse("policy.md", b"# Policy") == ("# Policy", "md")
