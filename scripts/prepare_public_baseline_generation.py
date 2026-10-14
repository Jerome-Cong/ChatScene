"""Prepare development query-only profiles for the two public baseline repositories."""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "benchmark/src"), str(ROOT / "scripts")]

from prepare_chatscene_runtime import binding, environment
from bus_benchmark.generation import generation_harness_paths, implementation_bundle_sha256, validate_implementation_bundle
from bus_benchmark.jsonio import read_json, read_jsonl, write_json, write_jsonl, sha256_file


def frozen_python(path):
    path = Path(path).absolute()
    frozen = path.with_name("python-frozen")
    original = path.resolve()
    if not frozen.exists():
        shutil.copy2(original, frozen)
    # Older uv Python builds use an origin-relative shared libpython loader.
    for library in (original.parent.parent / "lib").glob("libpython*.so*"):
        if library.is_file():
            target = frozen.parent.parent / "lib" / library.name
            if not target.exists():
                shutil.copy2(library.resolve(), target)
    if frozen.is_symlink() or not frozen.is_file():
        raise ValueError("frozen Python must be a regular executable")
    return frozen


def prepare(method, source, output, api_env, embedding_env=None, milvus_uri="http://127.0.0.1:19531", pilot=True):
    from dotenv import dotenv_values

    source = source.resolve()
    output = output.resolve()
    values = dotenv_values(api_env, interpolate=False)
    if not values.get("OPENAI_MODEL"):
        raise ValueError("generation profile is missing OPENAI_MODEL")
    if output.exists():
        raise FileExistsError("choose a fresh configuration directory")
    output.mkdir(parents=True)
    python = frozen_python(source / "runtimes" / method / ".venv/bin/python")
    method_env = environment(python)
    harness_env = environment(Path(sys.executable).absolute())
    write_json(output / "method-environment.json", method_env)
    write_json(output / "harness-environment.json", harness_env)
    argv = [str(python), str(source / "benchmark_query_adapter.py"), "--model-name", values["OPENAI_MODEL"]]
    files = {python, Path(sys.executable).absolute(), source / "benchmark_query_adapter.py", *generation_harness_paths()}
    if method == "chat2scenic":
        model = source / "runtimes/chat2scenic/models/all-MiniLM-L6-v2"
        if not model.exists():
            snapshots = source / "runtimes/chat2scenic/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/snapshots"
            candidates = [p for p in snapshots.iterdir() if p.is_dir()]
            if len(candidates) != 1:
                raise ValueError("choose one unambiguous downloaded native embedding snapshot")
            model.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(candidates[0], model, symlinks=False)
        if not (model / "modules.json").is_file():
            raise ValueError("native embedding snapshot is incomplete")
        catalog = output / "embedding-catalog.json"
        write_json(catalog, {"model_family": "sentence-transformers/all-MiniLM-L6-v2", "directory": str(model), "files": [binding(p) for p in sorted(model.rglob("*")) if p.is_file()]})
        files.add(catalog)
        argv += ["--milvus-uri", milvus_uri, "--embedding-catalog", str(catalog), "--max-tokens", "16384",
                 "--temperature", str(values.get("OPENAI_COMPAT_TEMPERATURE") or "0"), "--api-timeout", "180",
                 "--api-max-retries", str(values.get("OPENAI_COMPAT_MAX_RETRIES") or "2")]
        files.update(p for folder in (source / "core", source / "utils", model) for p in folder.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
        files.add(source / "requirements-benchmark.txt")
        files.add(ROOT / ".carla-runtime/baseline-preflight-20261014/chat2scenic/volumes-release.zip")
        artifact_type, artifact_path = "carla_scenic3_v0.1", "artifact.scenic"
    elif method == "text2scenario":
        embedding_env = Path(embedding_env or source / ".env.embedding").resolve()
        embedding = dotenv_values(embedding_env, interpolate=False)
        if not embedding.get("OPENAI_MODEL"):
            raise ValueError("embedding profile is missing OPENAI_MODEL")
        argv += ["--embedding-model", embedding["OPENAI_MODEL"], "--embedding-timeout", str(embedding.get("OPENAI_COMPAT_TIMEOUT_S") or "60"),
                 "--embedding-max-retries", str(embedding.get("OPENAI_COMPAT_MAX_RETRIES") or "2"), "--api-timeout", str(values.get("OPENAI_COMPAT_TIMEOUT_S") or "60"),
                 "--api-max-retries", str(values.get("OPENAI_COMPAT_MAX_RETRIES") or "2")]
        files.update(source / name for name in ("scenario_generator.py", "scenario_generator.ipynb", "converter.py", "RAG_querry.py", "scenario_components.yaml", "requirements.txt"))
        files.update(p for p in (source / "TemplateOpenScenario").rglob("*") if p.is_file())
        # The credential file is deliberately never included in a source bundle.
        artifact_type, artifact_path = "carla_xosc_bundle_v0.1", "artifact.json"
    else:
        raise ValueError("unknown public method")
    files.update(Path(item["path"]) for env in (method_env, harness_env) for item in env["python_environment_control_files"])
    bundle = {"status": "draft", "bundle_id": method + "-public-compatible-models-v0.1",
              "environment_manifest": binding(output / "method-environment.json"),
              "harness_environment_manifest": binding(output / "harness-environment.json"),
              "files": [binding(p) for p in sorted(files, key=str)]}
    bundle["bundle_sha256"] = implementation_bundle_sha256(bundle)
    config = {
        "schema_version": "0.1", "status": "draft", "method_id": method + "_carla_public_v0_1", "platform": "carla",
        "query_input_fields": ["query_text"], "post_output_semantic_repair": False, "implementation_bundle": bundle,
        "adapter": {"type": "command", "argv": argv, "artifact_path": artifact_path, "timeout_seconds": 1200,
                    "output_protocol": "stdout_json_v0.1", "environment_passthrough": ["PATH", "LANG", "LC_ALL", "OPENAI_API_KEY", "OPENAI_BASE_URL"],
                    "max_stdout_bytes": 8388608, "max_stderr_bytes": 8388608, "max_artifact_bytes": 16777216},
        "finalization": {"policy_id": "carla_ego_proxy_v0.1", "ego_blueprint": "vehicle.chevrolet.impala", "ego_length_m": 5.33,
                         "ego_width_m": 2.1, "artifact_type": artifact_type},
    }
    if method == "text2scenario":
        config["adapter"]["environment_passthrough"] += ["EMBEDDING_API_KEY", "EMBEDDING_BASE_URL"]
    validate_implementation_bundle(config, formal=False)
    write_json(output / "method.json", config)
    library = ROOT / "query_lib/bus_ego_topdown_2d_dev_query_library_v0_2.jsonl"
    roster = read_json(ROOT / "benchmark_artifacts/drafts/chatscene_carla_dev_roster_draft.json")
    if pilot:
        record = read_jsonl(library)[0]
        write_jsonl(output / "pilot-library.jsonl", [record])
        library = output / "pilot-library.jsonl"
        roster["queries"] = [r for r in roster["queries"] if r["query_id"] == record["query_id"]]
        roster.update(query_count=1, expected_response_count=5)
    roster.update(method_id=config["method_id"], library_path=str(library), library_sha256=sha256_file(library))
    write_json(output / "roster.json", roster)
    report = {"development_only": True, "method_id": config["method_id"], "generation_model": values["OPENAI_MODEL"],
              "artifact_type": artifact_type, "scope": "public-implementation-compatible-models", "paper_exact_reproduction": False,
              "judge_kernel_gate_retained": True, "credentials_in_bundle": False}
    if method == "text2scenario":
        report["embedding_model"] = embedding["OPENAI_MODEL"]
        report["public_demo_only"] = True
    else:
        report["embedding_model_family"] = "sentence-transformers/all-MiniLM-L6-v2"
        report["interaction_policy"] = "one mechanical confirm, no semantic feedback"
    write_json(output / "preparation.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=("chat2scenic", "text2scenario"), required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api-env", type=Path, default=ROOT / ".env")
    parser.add_argument("--embedding-env", type=Path)
    parser.add_argument("--milvus-uri", default="http://127.0.0.1:19531")
    parser.add_argument("--full-development", action="store_true")
    args = parser.parse_args()
    source = args.source or ROOT.parent / ("Chat2scenic" if args.method == "chat2scenic" else "Text2Scenario")
    print(json.dumps(prepare(args.method, source, args.output, args.api_env, args.embedding_env, args.milvus_uri, not args.full_development), indent=2))
