const express = require('express');
const router = express.Router();

const LLM_URL = process.env.LLM_URL || 'http://192.168.50.148:8080/v1/completions';
const LLM_TIMEOUT = 120000;

function buildPrompt(text) {
  // Make the format instruction immediately before the JSON prefix
  return `输出交易结构JSON。entities字段:id,name,role,type。relations字段:from,to,label,type。
type取值:party|asset|guarantee|special。rel type取值:transfer|payment|guarantee|control。

文本:${text}

{"entities":[{"id":"1","name":"`;
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
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), LLM_TIMEOUT);

    const response = await fetch(LLM_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        prompt: buildPrompt(text.trim()),
        max_tokens: 2000,
        temperature: 0,
        stream: false
      }),
      signal: controller.signal,
    });

    clearTimeout(timeout);

    if (!response.ok) {
      throw new Error(`LLM error: ${response.status}`);
    }

    const data = await response.json();
    let raw = data.choices?.[0]?.text || '';

    // Strip think tags
    raw = raw.replace(/<think>[\s\S]*?<\/think>/g, '').trim();

    // Add the prefix back to reconstruct full JSON
    let fullJson = '{"entities":[{"id":"1","name":"' + raw;

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
