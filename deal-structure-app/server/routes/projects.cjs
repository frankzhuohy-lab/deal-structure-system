const express = require('express');
const router = express.Router();
const { v4: uuidv4 } = require('uuid');
const db = require('../db.cjs');

router.get('/', (req, res) => {
  const projects = db.prepare('SELECT id, name, created_at, updated_at FROM projects ORDER BY updated_at DESC').all();
  res.json(projects);
});
router.post('/', (req, res) => {
  const { name, entities, relations } = req.body;
  const id = uuidv4();
  db.prepare('INSERT INTO projects (id, name, entities, relations) VALUES (?, ?, ?, ?)').run(id, name || '未命名项目', JSON.stringify(entities || []), JSON.stringify(relations || []));
  res.json({ id });
});
router.get('/:id', (req, res) => {
  const p = db.prepare('SELECT * FROM projects WHERE id = ?').get(req.params.id);
  if (!p) return res.status(404).json({ error: 'not found' });
  p.entities = JSON.parse(p.entities);
  p.relations = JSON.parse(p.relations);
  res.json(p);
});
router.put('/:id', (req, res) => {
  const { name, entities, relations } = req.body;
  db.prepare("UPDATE projects SET name = ?, entities = ?, relations = ?, updated_at = datetime('now') WHERE id = ?").run(name, JSON.stringify(entities), JSON.stringify(relations), req.params.id);
  res.json({ ok: true });
});
router.delete('/:id', (req, res) => {
  db.prepare('DELETE FROM projects WHERE id = ?').run(req.params.id);
  res.json({ ok: true });
});
module.exports = router;
