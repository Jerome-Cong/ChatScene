# Bus Scene Benchmark

`bus-scene-benchmark` is a standalone, query-only evaluation harness for
CARLA and MetaDrive scene-generation methods. It contains the immutable schema
set and v0.2 query libraries; it does **not** contain ChatScene, CARLA,
MetaDrive, a model, credentials, or generated results.

## Install

```bash
python -m pip install ./benchmark
bus-benchmark paths
```

The `paths` command prints the installed query-library and schema locations so
shell scripts never need to assume this repository's layout. Set
`BUS_BENCHMARK_WORKSPACE` to a directory you own when using review or freeze
commands that need caller-owned drafts and outputs. If unset, the current
directory is used.

## Connect another CARLA method

Use the generic `command` adapter. The harness starts the configured executable
in a new work directory once per query, supplies only the UTF-8 `query_text` on
standard input, and collects either a declared artifact or a small JSON status
object. The method never receives oracle, roster, score, or run metadata.

For an artifact-producing method, configure:

```json
{
  "type": "command",
  "argv": ["/absolute/path/to/your-generator"],
  "artifact_path": "result.scenic",
  "output_protocol": "artifact_on_zero",
  "timeout_seconds": 600,
  "environment_passthrough": [],
  "max_stdout_bytes": 1048576,
  "max_stderr_bytes": 1048576,
  "max_artifact_bytes": 4194304
}
```

Your process reads one query from stdin and writes `result.scenic` beneath its
working directory. Put this adapter in a complete method configuration that
conforms to `method_config.schema.json`, then run `generate` with an explicit
library, roster, method configuration, and output directory. Runtime settings
remain separate and must point at the CARLA installation chosen by the method
owner.

The historic `chatscene_legacy` adapter remains available only as a backward
compatibility integration; it is not required by, or bundled into, this
package.
