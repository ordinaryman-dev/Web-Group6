# Hero Hotel 测试报告（2026-11-21）

本目录是 M2 的第二阶段测试交付物，对应被测版本 `main @ 17a21e4`
（“重构页面代码结构并补充全项目中文注释”）。

## 文件

| 文件 | 说明 |
| --- | --- |
| `测试报告.pdf` | 正式交付版，25 页，含封面、目录、16 张图表/截图；F1 已修复并更新验证 |
| `测试报告.docx` | 可编辑版；目录是 Word 域，右键“更新域”可刷新页码 |
| `测试报告.html` | 报告源文件，`loadtest/tools/html_to_docx.py` 的输入 |
| `assets/charts/` | 由 `loadtest/tools/make_charts.py` 从原始 JSON 生成的 10 张图表 |
| `assets/screenshots/` | 浏览器实际访问的 6 张页面截图 |

## 与测试代码的关系

测试代码在仓库根目录 `loadtest/`，与报告目录分开：

- `loadtest/run_conflict.py`：同房抢订、幂等、假售罄探针
- `loadtest/run_stress.py` / `run_ramp.py`：S-browse / S-book / S-mixed 与阶梯压测
- `loadtest/run_seed_check.py`：F1 种子数据回归检查（9 条）
- `loadtest/run_dbcheck.py`：I1–I15 一致性 SQL
- `loadtest/lib/`、`loadtest/scenarios/`：HTTP 流程、指标、夹具、场景
- `loadtest/results/`：原始 CSV/JSON、pytest 基线、F1 证据、最终检查输出

## 重新生成

```powershell
# 1) 重新生成图表
python loadtest/tools/make_charts.py --results loadtest/results `
  --output "<本目录>/assets/charts"

# 2) 从 HTML 重新排版 DOCX，并调用 LibreOffice 导出 PDF
python loadtest/tools/html_to_docx.py --html "<本目录>/测试报告.html" `
  --output "<本目录>/测试报告.docx"

# 3) 可选：文档自检
python "<docx-skill>/scripts/postcheck.py" "<本目录>/测试报告.docx"
```

排版脚本只读取 `测试报告.html` 的语义结构；正文、表格数字和图片说明都以 HTML 为准。
修改报告内容后重新运行上面两条命令即可同步 PDF。
