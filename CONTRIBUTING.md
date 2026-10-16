<!-- SPDX-License-Identifier: Apache-2.0 -->
# Contributing

Thank you for helping. Two rules:

1. **License.** Your contribution is licensed under Apache-2.0, the same as the project (inbound = outbound, Apache-2.0 section 5). There is no CLA to sign.
2. **Sign-off.** Add a `Signed-off-by:` line to every commit (`git commit -s`). It certifies the [Developer Certificate of Origin](https://developercertificate.org/): you wrote the change or have the right to submit it.

Never commit tokens, keys, internal hostnames or customer data. Examples use placeholders such as `<TOKEN_FILE>` and `example.com`.

## 依赖锁文件

`deploy/requirements.in` 保留应用、迁移服务和适配器的完整精确版本约束。不要直接编辑 `deploy/requirements.txt` 的版本或哈希；版本升级须作为单独变更审阅。

使用已安装 `pip-tools==7.6.2`、`pip==26.2.1` 的独立 Python 3.12 工具环境再生锁文件，不要升级运行或测试环境中的工具：

```sh
# SPDX-License-Identifier: Apache-2.0
"<TOOLS_VENV>/bin/python" scripts/compile_requirements.py
```

脚本调用 `pip-compile --generate-hashes`，保留 SPDX，并抑制索引地址与工具环境的绝对路径。索引认证只通过本地既有安全配置提供，不写入锁文件或仓库。提交前确认版本约束未改变，并在 Python 3.12 的目标平台以 `pip install --require-hashes --only-binary=:all: -r deploy/requirements.txt` 完成干净安装；Dockerfile 与所有客户端安装说明也必须保留这两个选项。只安装 wheel 可避免源发行包在构建时另外解析未锁定依赖。当前目标包括 macOS arm64 与 Linux arm64；依赖变更后须重新核验两者，不能将单平台解析视为跨平台保证。

依据：[pip-tools 哈希生成与跨环境说明](https://pip-tools.readthedocs.io/en/stable/)、[pip 安全安装](https://pip.pypa.io/en/stable/topics/secure-installs/)。
