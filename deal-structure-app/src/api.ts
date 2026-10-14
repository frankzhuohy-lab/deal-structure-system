import { Entity, Relation, Project, ProjectDetail } from './types'

const BASE = 'api'  // 相对路径：兼容工作站 /apps/deal/ 子路径反代

export async function aiParse(text: string): Promise<{ entities: Entity[]; relations: Relation[] }> {
  const r = await fetch(`${BASE}/ai/parse`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
  })
  if (!r.ok) {
    const err = await r.json().catch(() => ({ error: 'AI解析失败' }))
    throw new Error(err.error || 'AI解析失败')
  }
  return r.json()
}

export async function listProjects(): Promise<Project[]> {
  const r = await fetch(`${BASE}/projects`); return r.json()
}
export async function getProject(id: string): Promise<ProjectDetail> {
  const r = await fetch(`${BASE}/projects/${id}`)
  if (!r.ok) throw new Error('not found'); return r.json()
}
export async function createProject(data: { name?: string; entities?: Entity[]; relations?: Relation[] }): Promise<{ id: string }> {
  const r = await fetch(`${BASE}/projects`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })
  return r.json()
}
export async function updateProject(id: string, data: { name?: string; entities?: Entity[]; relations?: Relation[] }) {
  await fetch(`${BASE}/projects/${id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })
}
export async function deleteProject(id: string) {
  await fetch(`${BASE}/projects/${id}`, { method: 'DELETE' })
}
