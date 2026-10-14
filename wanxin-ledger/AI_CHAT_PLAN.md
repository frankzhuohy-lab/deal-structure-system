# 台账系统内置 AI 助手 - 实施步骤

## 目标
在皖信台账系统右下角加一个 AI 聊天助手，用户用自然语言查账，后台调 DeepSeek API 回答。

## 环境
- 路径: `C:\Users\frank\Desktop\皖信台账系统\`
- 后端: `app.py` (单文件 Flask)
- 前端: `templates/index.html` (单文件 SPA)
- Python: `.venv\Scripts\python.exe` (已装 Flask, requests)
- 端口: 8090

---

## 步骤 1: 申请 DeepSeek API Key

1. 打开 https://platform.deepseek.com/
2. 注册/登录，充值 10 块钱够用一年
3. 创建 API Key，形如 `sk-xxxxxxxx`
4. 暂存在脑子里，后续步骤用

---

## 步骤 2: 后端 — `app.py` 加聊天路由

在 `app.py` 末尾（`if __name__ == '__main__'` 之前）添加以下代码块：

```python
# ========== AI 聊天助手 ==========
import requests as req_http

DEEPSEEK_KEY = "sk-你的key填这里"  # ← 替换为你的真实 API Key

@app.route('/api/chat', methods=['POST'])
def ai_chat():
    """AI 助手：自然语言查台账"""
    data = request.get_json()
    question = data.get('question', '').strip()
    if not question:
        return jsonify({"error": "问题不能为空"})

    # 1. 收集上下文数据
    context_parts = []

    # 项目概览
    projects = db.execute("""
        SELECT p.id, p.project_name, p.investment_amount, p.annual_rate,
               p.loan_term_months, p.repayment_method, p.status,
               c.short_name as customer_name
        FROM projects p LEFT JOIN customers c ON p.customer_id=c.id
    """).fetchall()
    context_parts.append("=== 项目列表 ===")
    for p in projects:
        context_parts.append(
            f"{p['id']} | {p['project_name']} | 客户:{p['customer_name']} | "
            f"金额:{p['investment_amount']}万 | 利率:{p['annual_rate']*100 if p['annual_rate'] else 0:.1f}% | "
            f"期限:{p['loan_term_months']}月 | 状态:{p['status']}"
        )

    # 近期付息（未付的）
    pending = db.execute("""
        SELECT ps.*, p.project_name, c.short_name as customer_name
        FROM payment_schedules ps
        JOIN projects p ON ps.project_id=p.id
        LEFT JOIN customers c ON p.customer_id=c.id
        WHERE ps.status='pending'
        ORDER BY ps.due_date LIMIT 30
    """).fetchall()
    context_parts.append("\n=== 待付利息 ===")
    for pp in pending:
        context_parts.append(
            f"{pp['project_name']} | 第{pp['period_number']}期 | "
            f"到期:{pp['due_date']} | 利息:{pp['interest_amount']} | 本金:{pp['principal_amount']}"
        )

    # 客户信息
    customers = db.execute("SELECT * FROM customers").fetchall()
    context_parts.append("\n=== 客户信息 ===")
    for c in customers:
        context_parts.append(
            f"{c['id']} | {c['short_name'] or c['name']} | "
            f"行业:{c['industry'] or '-'} | 评级:{c['credit_rating'] or '-'}"
        )

    context = '\n'.join(context_parts)
    # 防止上下文过长
    if len(context) > 8000:
        context = context[:8000] + "\n...(数据截断)"

    # 2. 调用 DeepSeek
    try:
        resp = req_http.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": "deepseek-chat",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是皖信投资的台账AI助手。根据下方数据回答用户问题。\n"
                            "规则：\n"
                            "1. 金额单位是万元\n"
                            "2. 利率是小数，需乘以100显示为百分比\n"
                            "3. 只回答数据中有的信息，不要编造\n"
                            "4. 回答简洁，重点用数字说话\n\n"
                            f"数据：\n{context}"
                        )
                    },
                    {"role": "user", "content": question}
                ],
                "temperature": 0.3,
                "max_tokens": 1000
            },
            timeout=30
        )
        if resp.status_code == 200:
            answer = resp.json()['choices'][0]['message']['content']
            return jsonify({"success": True, "answer": answer})
        else:
            return jsonify({"error": f"AI 服务异常: {resp.status_code}"})
    except Exception as e:
        return jsonify({"error": f"请求失败: {str(e)}"})
```

---

## 步骤 3: 前端 — `templates/index.html` 加聊天 UI

### 3a. 在 `<style>` 标签末尾（`</style>` 之前）添加：

```css
/* ======== AI 助手 ======== */
#aiChatBtn {
    position: fixed; bottom: 24px; right: 24px;
    width: 56px; height: 56px; border-radius: 50%;
    background: linear-gradient(135deg, #0ea5e9, #6366f1);
    color: #fff; font-size: 24px; border: none; cursor: pointer;
    box-shadow: 0 4px 20px rgba(14,165,233,.35);
    z-index: 9999; display: flex; align-items: center; justify-content: center;
    transition: transform .2s;
}
#aiChatBtn:hover { transform: scale(1.1); }

#aiPanel {
    position: fixed; bottom: 90px; right: 24px;
    width: 400px; height: 520px; background: #fff;
    border-radius: 16px; box-shadow: 0 8px 40px rgba(0,0,0,.12);
    z-index: 9998; display: none; flex-direction: column;
    overflow: hidden; border: 1px solid #e2e8f0;
}
#aiPanel.open { display: flex; }

#aiHeader {
    padding: 16px 20px; background: linear-gradient(135deg, #0ea5e9, #6366f1);
    color: #fff; font-size: 15px; font-weight: 600;
    display: flex; justify-content: space-between; align-items: center;
}
#aiHeader button { background: none; border: none; color: #fff; font-size: 20px; cursor: pointer; }

#aiMessages {
    flex: 1; overflow-y: auto; padding: 16px;
}
.ai-msg { margin-bottom: 14px; display: flex; gap: 8px; }
.ai-msg.user { flex-direction: row-reverse; }
.ai-msg .avatar {
    width: 32px; height: 32px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center; font-size: 14px;
}
.ai-msg.assistant .avatar { background: #e0f2fe; color: #0ea5e9; }
.ai-msg.user .avatar { background: #e0e7ff; color: #6366f1; }
.ai-msg .bubble {
    max-width: 80%; padding: 10px 14px; border-radius: 12px;
    font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word;
}
.ai-msg.assistant .bubble { background: #f1f5f9; color: #1e293b; }
.ai-msg.user .bubble { background: #0ea5e9; color: #fff; }

#aiInput {
    padding: 12px 16px; border-top: 1px solid #e2e8f0; display: flex; gap: 8px;
}
#aiInput input {
    flex: 1; padding: 10px 14px; border: 1px solid #e2e8f0;
    border-radius: 20px; font-size: 13px; outline: none;
}
#aiInput input:focus { border-color: #0ea5e9; }
#aiInput button {
    padding: 8px 16px; background: #0ea5e9; color: #fff;
    border: none; border-radius: 20px; font-size: 13px; cursor: pointer;
}
.ai-typing { color: #94a3b8; font-size: 12px; padding: 4px 14px; }
```

### 3b. 在 `</body>` 之前添加 HTML：

```html
<!-- AI 助手 -->
<button id="aiChatBtn" onclick="toggleAiChat()" title="AI 助手">💬</button>

<div id="aiPanel">
    <div id="aiHeader">
        <span>🤖 台账 AI 助手</span>
        <button onclick="toggleAiChat()">✕</button>
    </div>
    <div id="aiMessages">
        <div class="ai-msg assistant">
            <div class="avatar">🤖</div>
            <div class="bubble">你好！我是台账助手。\n可以问我：\n• 本月应收多少利息？\n• 哪些项目快到期了？\n• 皖创环保的付息情况？</div>
        </div>
    </div>
    <div id="aiInput">
        <input id="aiInputText" placeholder="输入问题..." onkeydown="if(event.key==='Enter')sendAiMsg()">
        <button onclick="sendAiMsg()">发送</button>
    </div>
</div>
```

### 3c. 在 `<script>` 标签内（`</script>` 之前）添加 JS：

```javascript
// ======== AI 聊天 ========
function toggleAiChat() {
    document.getElementById('aiPanel').classList.toggle('open');
    if (document.getElementById('aiPanel').classList.contains('open')) {
        document.getElementById('aiInputText').focus();
    }
}

async function sendAiMsg() {
    const input = document.getElementById('aiInputText');
    const question = input.value.trim();
    if (!question) return;

    const msgs = document.getElementById('aiMessages');

    // 显示用户消息
    msgs.insertAdjacentHTML('beforeend',
        `<div class="ai-msg user"><div class="avatar">👤</div><div class="bubble">${escapeHtml(question)}</div></div>`);
    input.value = '';

    // 显示输入中
    const typingId = 'typing_' + Date.now();
    msgs.insertAdjacentHTML('beforeend', `<div class="ai-typing" id="${typingId}">思考中...</div>`);
    msgs.scrollTop = msgs.scrollHeight;

    try {
        const res = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question })
        });
        const data = await res.json();

        // 移除"思考中"
        const typing = document.getElementById(typingId);
        if (typing) typing.remove();

        if (data.success) {
            msgs.insertAdjacentHTML('beforeend',
                `<div class="ai-msg assistant"><div class="avatar">🤖</div><div class="bubble">${escapeHtml(data.answer)}</div></div>`);
        } else {
            msgs.insertAdjacentHTML('beforeend',
                `<div class="ai-msg assistant"><div class="avatar">🤖</div><div class="bubble" style="color:#ef4444">${escapeHtml(data.error || '出错了')}</div></div>`);
        }
    } catch (e) {
        const typing = document.getElementById(typingId);
        if (typing) typing.remove();
        msgs.insertAdjacentHTML('beforeend',
            `<div class="ai-msg assistant"><div class="avatar">🤖</div><div class="bubble" style="color:#ef4444">网络错误: ${escapeHtml(e.message)}</div></div>`);
    }
    msgs.scrollTop = msgs.scrollHeight;
}

function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML.replace(/\n/g, '<br>');
}
```

---

## 步骤 4: 验证

```bash
# 启动服务器
cd /c/Users/frank/Desktop/皖信台账系统
.venv/Scripts/python.exe app.py
```

1. 浏览器打开 `http://localhost:8090`
2. 右下角出现蓝色 💬 按钮
3. 点击打开聊天窗口
4. 输入"本月应收多少利息？" 测试

---

## 注意事项

1. **DEEPSEEK_KEY 必须替换**为真实 key，否则 /api/chat 返回错误
2. 服务器必须安装了 `requests` — 通常 Flask 项目已有，没有就 `pip install requests`
3. 改完 `app.py` 要重启服务器
4. 改完 `index.html` 刷新浏览器即可
5. 不要改 `if __name__ == '__main__':` 之前已有的任何代码
