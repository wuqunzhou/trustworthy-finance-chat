# 可信金融投研助手 - 独立聊天网页

这是一个非常薄的网页壳：浏览器只和本项目后端通信，后端再调用已经部署好的 Coze Workflow API。

## 环境变量

- `COZE_API_TOKEN`：在 Coze 部署页生成的 API Token。**不要写入前端或提交到 Git。**
- `COZE_API_URL`：默认已设置为 `https://bysb7qrsyw.coze.site/run`
- `PUBLIC_BASE_URL`：部署后填写本网页的公开地址，例如 `https://your-app.onrender.com`。上传 PDF 时，Coze 需要通过这个公网地址下载文件。

## 本地运行

本地可以测试“纯文字对话”，但本地上传 PDF 时，Coze 无法访问 `localhost` 的文件 URL。因此 PDF 上传请在 Render 等公网环境部署后测试。

```bash
pip install -r requirements.txt
set COZE_API_TOKEN=你的Token     # Windows CMD
python app.py
```

浏览器打开 `http://127.0.0.1:8000`。

## Render 部署

1. 把本文件夹上传到 GitHub 仓库。
2. 在 Render 新建 Web Service，连接仓库。
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn -b 0.0.0.0:$PORT app:app`
5. 添加环境变量 `COZE_API_TOKEN`，值从 Coze 部署页生成。
6. 首次部署成功后，把 Render 公网地址填到环境变量 `PUBLIC_BASE_URL`，例如 `https://trustworthy-finance-chat.onrender.com`，然后重新部署。
7. 打开公网地址即可聊天、上传 PDF。

## 安全说明

- API Token 只存在服务端环境变量，浏览器看不到。
- 上传的 PDF 会保存到部署实例临时磁盘；此版本用于比赛演示，不作为长期文档存储方案。
- 不要把 `.env`、Token、密钥提交到 GitHub。
