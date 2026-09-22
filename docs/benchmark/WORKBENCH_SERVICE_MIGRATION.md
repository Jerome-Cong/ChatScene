# BW-01：显式审阅服务与旧入口迁移

核心入口为 `bus_benchmark.review_context.ReviewContext`。调用者必须传入 library、oracle、已存在且可写的 workspace 和 reviewer 标签；路径错误直接报错，不搜索另一套题库，也不创建默认 assignment。状态禁止写入包目录及 site-packages。

```python
from pathlib import Path
from bus_benchmark.review_context import ReviewContext

context = ReviewContext(
    library_source=Path("/absolute/source/development.jsonl"),
    oracle_source=Path("/absolute/source/oracle.jsonl"),
    workspace=Path("/absolute/review-workspace"),
    reviewer_id="assigned-reviewer",
    split="my-development-suite",
    expected_subjects=12,
)
session = context.open_session()
```

`expected_subjects` 是调用者可选的 manifest 断言；没有声明时不要求 48/252。suite 标签与来源解耦，仍由现有正式 validator 检查 query/oracle 语义和源绑定。此类接口本身不能认证操作者是人，外部真实人工控制责任不变。

维护者也可运行 `python -m bus_benchmark.review_context --library ... --oracle ... --workspace ... --reviewer ...`；只准备或恢复草稿 checkpoint，输出明确 `human_gold: false`。从仓库外运行需先安装包或显式设置源码导入路径；真正脱离 checkout 的 wheel 验收安排在 BW-09，不以 PYTHONPATH 测试冒充安装验收。

## 职责分离

- `review_context.py`：显式来源、workspace 和 manifest 校验。
- `review_forms.py` / `review_vocabulary.py`：原表单转换、共享原因与标签；正式校验仍由 `human_workflow` 负责。
- `review_session.py` / `review_store.py`：草稿、明确完成、锁与并发比较后写入；finalizer 是唯一 gold 出口。
- `review_presentation.py`：原有可读展示；字段完整性增强属于 BW-02。
- `review_legacy.py` / `query_review_workbench.py`：原 checkout assignment 适配和 notebook UI，原 public 名称继续可导入。

旧 `ensure_review_assignment`、`from_assignment` 和 notebook 仍使用现有 48/252 manifest、导出脚本和 Agent 源绑定；这是兼容入口，不能作为可移植入口。原 CSS 和 UI 操作不变。测试中的文件写入故障注入改到新的 `review_session.write_jsonl` 所在位置，不再 patch notebook 模块的内部 I/O 名称。

核心没有 ipywidgets/IPython 依赖。当前维护者存储层保留已有 POSIX `flock` 和原子持久化行为；没有宣称 Python 维护者程序已通过 Windows 验收。计划中的 Windows 用户入口是后续离线浏览器题包。

两份当前源码暂时同步；唯一源码收敛按 BW-09 执行，不在本包隐式删除安装源码树。旧 checkpoint schema、canonical payload、确认与定稿边界不变，无须迁移历史进度。
