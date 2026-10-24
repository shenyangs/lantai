# 澜台基础体验不回退棘轮

## 最高原则

`PRESERVE-001`：任何修复、重构、优化、依赖升级和新功能，都不得无声破坏此前已经正确、稳定且被用户认可的体验。无法证明不回退的改动，不得合并或发布。

## 文件职责

- `contracts.json`：受保护体验契约和现有验证入口。
- `incidents.json`：已经暴露过的历史事故与契约映射。
- `scripts/check_foundation_experience_gate.py`：本地、CI 和正式发布共用的 fail-closed 检查器。

## 只允许增强

与基线版本比较时，以下操作会直接失败：

- 删除受保护契约。
- 降低 P0/P1 严重级别。
- 取消发布阻断。
- 移除受保护界面、可观测不变量、禁止回归项或验证入口。
- 删除历史事故、事故契约映射、fixture 或 replay target。

新增契约、新增禁止回归项和新增验证是允许的。如果产品方向确实要取代旧体验，应新增版本化契约和迁移证据，不应直接把旧契约改弱。

## 验证

```bash
python3 scripts/check_foundation_experience_gate.py
python3 scripts/check_foundation_experience_gate.py --run-verifications
python3 -m pytest tests/unit/scripts/test_foundation_experience_gate.py -q
```

在所有 Pull Request 和主分支提交中，检查器会与基线提交比较，并真正执行所有受保护行为测试；正式桌面构建也会在签名、公证和上传前重新执行。
