const express = require('express');
const router = express.Router();

// 配置（环境变量优先）：
//   新模式（OpenAI 兼容 chat completions）：
//     LLM_BASE_URL=https://api.deepseek.com/v1  LLM_API_KEY=sk-...  LLM_MODEL=deepseek-chat
//   旧模式（llama.cpp /v1/completions）：
//     LLM_URL=http://192.168.50.148:8080/v1/completions
const LLM_BASE_URL = (process.env.LLM_BASE_URL || '').replace(/\/$/, '');
const LLM_API_KEY = process.env.LLM_API_KEY || '';
const LLM_MODEL = process.env.LLM_MODEL || '';
const LLM_URL = process.env.LLM_URL || '';
const LLM_TIMEOUT = 120000;

function buildPrompt(text) {
  return `输出交易结构JSON。entities字段:id,name,role,type。relations字段:from,to,label,type。
type取值:party|asset|guarantee|special。rel type取值:transfer|payment|guarantee|control。
只输出JSON，不要输出解释、Markdown或代码块。

文本:${text}

JSON:`;
}

async function callLlm(prompt) {
  if (LLM_BASE_URL) {
    const r = await fetch(LLM_BASE_URL + '/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(LLM_API_KEY ? { Authorization: 'Bearer ' + LLM_API_KEY } : {}),
      },
      body: JSON.stringify({
        model: LLM_MODEL,
        messages: [{ role: 'user', content: prompt }],
        temperature: 0,
        stream: false,
      }),
    });
    if (!r.ok) throw new Error(`LLM error: ${r.status}`);
    const d = await r.json();
    return d.choices?.[0]?.message?.content || '';
  }
  if (LLM_URL) {
    const r = await fetch(LLM_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: `输出交易结构JSON。entities字段:id,name,role,type。relations字段:from,to,label,type。
type取值:party|asset|guarantee|special。rel type取值:transfer|payment|guarantee|control。

文本:${prompt.split('文本:')[1] || prompt}

{"entities":[{"id":"1","name":"`,
        max_tokens: 2000,
        temperature: 0,
        stream: false,
      }),
    });
    if (!r.ok) throw new Error(`LLM error: ${r.status}`);
    const d = await r.json();
    const raw = (d.choices?.[0]?.text || '').replace(/<think>[\s\S]*?<\/think>/g, '').trim();
    return '{"entities":[{"id":"1","name":"' + raw;
  }
  throw new Error('未配置LLM：请设置 LLM_BASE_URL+LLM_API_KEY+LLM_MODEL 或 LLM_URL');
}

async function normalizeJson(jsonStr) {
  // The model might output various formats. Try to parse and normalize.
  let parsed;
  try {
    parsed = JSON.parse(jsonStr);
  } catch (e) {
    return null;
  }
  
  if (!parsed.entities && !parsed.relations) {
    // Try alternative formats
    if (Array.isArray(parsed)) {
      // Model might have output just the entities array
      return { entities: parsed, relations: [] };
    }
    return null;
  }
  
  // Normalize entities
  parsed.entities = (parsed.entities || []).map((e, i) => ({
    id: e.id || String(i + 1),
    name: e.name || e.entity_value || e.label || '未知',
    role: e.role || e.entity_type || '主体',
    type: ['party', 'asset', 'guarantee', 'special'].includes(e.type) ? e.type : 'party'
  }));
  
  // Normalize relations
  parsed.relations = (parsed.relations || []).map(r => ({
    from: r.from || '1',
    to: r.to || '2',
    label: r.label || r.relation || '关系',
    type: ['transfer', 'payment', 'guarantee', 'control'].includes(r.type) ? r.type : 'transfer'
  }));
  
  return parsed;
}

router.post('/parse', async (req, res) => {
  const { text } = req.body;
  if (!text || !text.trim()) {
    return res.status(400).json({ error: '请输入交易结构描述' });
  }

  try {
    const responseText = await callLlm(buildPrompt(text.trim()));
    let fullJson = (responseText || '').trim()
      .replace(/^```(json)?\s*/i, '').replace(/\s*```$/, '');

    // Find balanced JSON object (most outer)
    let balanced = '', depth = 0, start = -1;
    for (let i = 0; i < fullJson.length; i++) {
      if (fullJson[i] === '{') {
        if (depth === 0) start = i;
        depth++;
      } else if (fullJson[i] === '}') {
        depth--;
        if (depth === 0 && start >= 0) {
          balanced = fullJson.substring(start, i + 1);
          break;
        }
      }
    }

    if (!balanced) {
      return res.status(422).json({ error: '模型未返回有效JSON', raw: raw.substring(0, 300) });
    }

    const parsed = await normalizeJson(balanced);
    if (!parsed) {
      return res.status(422).json({ error: 'JSON格式不符合预期', raw: balanced.substring(0, 300) });
    }

    if (parsed.entities.length === 0) {
      return res.status(422).json({ error: '未识别到任何交易主体' });
    }

    res.json(parsed);
  } catch (err) {
    if (err.name === 'AbortError') {
      return res.status(504).json({ error: 'AI解析超时，请重试' });
    }
    console.error('AI parse error:', err);
    res.status(500).json({ error: err.message || 'AI解析失败' });
  }
});

module.exports = router;
