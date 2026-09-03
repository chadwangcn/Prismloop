# Prismloop APP Case Library

`cases/` 是 APP 测试工程师长期维护的业务测试资产，不是 Prismloop 工具测试目录。

```text
cases/
├── index.json          唯一发现入口
├── schema/             Case 与 Case Set schema
├── suites/             smoke、regression、figma、media、permissions 等套件
├── fixtures/           脱敏 fixture 或不可变 artifact 引用
└── baselines/          Figma node/version 与视觉基线 artifact 引用
```

Case 不能保存 Figma Token、云手机凭证、本地文件绝对路径或临时签名 URL。真实执行结果放入 Artifact Store；本目录只保存可重放的业务意图、步骤、断言和证据要求。

新增 Case 时同时更新 `index.json`，并使用 `schema/app-case.schema.json` 校验。运行 `python3 scripts/validate_cases.py` 检查索引、重复 ID、遗漏文件和明显秘密。Case ID 的业务含义发生变化时创建新 ID，并在旧 Case 中记录 superseded 关系。
