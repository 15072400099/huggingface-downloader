# 开发与反馈说明

## 这个项目不接收代码修改

**Pull Request 不会被合并。** 这不是客套话，是真的不合并，避免你白写。

欢迎的：

- 提 [Issue](https://github.com/15072400099/huggingface-downloader/issues/new/choose) 说清遇到的问题
- 使用体验上的建议：哪个按钮多余、哪句提示看不懂
- 兼容性反馈：你在哪个镜像地址、什么网络环境下跑出了什么现象

不接收的：

- 代码 PR（包括「顺手修个 typo」）
- Dependabot 之类的自动依赖升级 PR

想自己改着用完全可以：MIT 许可允许你 fork 任意修改，只是不会合并回本仓库。

---

## 如果你要自己在本地跑起来改

需要 Windows 10/11 + Python 3.10 以上。**没有第三方依赖**，不需要 pip install：

```bat
启动下载器.bat
```

或者直接用系统 Python：

```bat
python app\server.py
```

界面在 <http://127.0.0.1:8766>。改前端不用重启（刷新页面），改后端要重启。

### 代码结构

```
app/server.py            HTTP 服务与路由（标准库，无框架）
app/download_manager.py  任务队列、进度、暂停续传、本地模型库
app/hf_client.py         HuggingFace 接口封装与错误翻译
app/transport.py         HTTP 传输层（调用系统 curl）
app/verify_models.py     和远端清单比对
app/repair_missing.py    只补缺失的分片
```

### 三个必须知道的坑

**1. `.bat` 文件必须是 GBK 编码。**
`cmd.exe` 按系统 ANSI 代码页解析批处理，存成 UTF-8 会直接报「'到' 不是内部或外部命令」。
`.gitattributes` 已把 `*.bat` 标记为 `-text`，git 不会转换它的字节。

**2. 不要提交 `models/`、`data/`、`python/`、`.venv/`、`build/`、`dist/`。**
都在 `.gitignore` 里。模型动辄几十 GB。

**3. 只监听 `127.0.0.1`。**
本程序没有鉴权，却带有删除文件夹的接口。改成 `0.0.0.0` 等于把删文件的权限开放给局域网。

### 关于传输层

`transport.py` 故意不用 `requests`：在实测网络环境下，Python 的 TLS 握手会被对端重置，
而 curl 正常。改动这里时请保持「优先用系统 curl」这个前提，并注意
Windows 自带 curl 8.4 不带 zlib，**不能用 `--compressed`**。

### 提交前自测

- [ ] 能启动，界面能打开
- [ ] 搜索能出结果
- [ ] 下载一个小文件（如 `Qwen/Qwen2.5-0.5B-Instruct` 的 `config.json`）能完成
- [ ] 下载中点「暂停」再点「继续」，进度没有归零
- [ ] 设置页「测试连接」能返回延迟
