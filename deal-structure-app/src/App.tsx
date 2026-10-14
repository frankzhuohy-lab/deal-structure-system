import { useState, useEffect, useCallback } from 'react'
import { Download, Save, FolderOpen, Trash2, Plus } from 'lucide-react'
import { ReactFlowProvider } from '@xyflow/react'
import InputPanel from './components/InputPanel'
import Canvas from './components/Canvas'
import { Entity, Relation, Project } from './types'
import { listProjects, createProject, getProject, updateProject, deleteProject } from './api'
import { NINGGUO_TEMPLATE } from './data/ningguoTemplate'

function exportPNG() {
  const svgEl = document.querySelector('.react-flow__renderer svg') as SVGSVGElement | null
  if (!svgEl) { alert('无内容可导出'); return }
  const svgData = new XMLSerializer().serializeToString(svgEl)
  const c = document.createElement('canvas'); const ctx = c.getContext('2d')!
  const img = new Image()
  img.onload = () => { c.width = img.width * 2; c.height = img.height * 2; ctx.scale(2, 2); ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, c.width, c.height); ctx.drawImage(img, 0, 0); const a = document.createElement('a'); a.download = '交易结构图.png'; a.href = c.toDataURL('image/png'); a.click() }
  img.src = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(svgData)))
}

export default function App() {
  // 默认加载宁国模板
  const [entities, setEntities] = useState<Entity[]>(NINGGUO_TEMPLATE.entities)
  const [relations, setRelations] = useState<Relation[]>(NINGGUO_TEMPLATE.relations)
  const [projects, setProjects] = useState<Project[]>([])
  const [currentId, setCurrentId] = useState<string | null>(null)
  const [projectName, setProjectName] = useState(NINGGUO_TEMPLATE.name)
  const [dirty, setDirty] = useState(false)
  const [showProjects, setShowProjects] = useState(false)

  // Load project list
  useEffect(() => { listProjects().then(setProjects).catch(() => {}) }, [])

  // Auto-save
  useEffect(() => {
    if (!dirty || !currentId) return
    const t = setTimeout(() => {
      updateProject(currentId, { name: projectName || '未命名', entities, relations }).then(() => {
        setDirty(false); listProjects().then(setProjects).catch(() => {})
      }).catch(() => {})
    }, 3000)
    return () => clearTimeout(t)
  }, [dirty, entities, relations, currentId, projectName])

  const markDirty = useCallback(() => setDirty(true), [])

  const handleSave = async () => {
    if (currentId) {
      await updateProject(currentId, { name: projectName || '未命名', entities, relations })
    } else {
      const { id } = await createProject({ name: projectName || '未命名', entities, relations })
      setCurrentId(id)
    }
    setDirty(false)
    listProjects().then(setProjects).catch(() => {})
  }

  const handleLoad = async (id: string) => {
    const p = await getProject(id)
    setEntities(p.entities); setRelations(p.relations)
    setCurrentId(p.id); setProjectName(p.name); setDirty(false)
  }

  const handleNew = () => {
    setEntities([]); setRelations([]); setCurrentId(null); setProjectName(''); setDirty(false)
  }

  const handleDelete = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation()
    await deleteProject(id)
    if (currentId === id) { setCurrentId(null); setProjectName('') }
    listProjects().then(setProjects).catch(() => {})
  }

  return (
    <div className="flex h-screen bg-gray-50">
      <InputPanel entities={entities} setEntities={(fn) => { setEntities(fn as any); markDirty() }}
        relations={relations} setRelations={(fn) => { setRelations(fn as any); markDirty() }} activeTab="" setActiveTab={() => {}} />

      <div className="flex-1 flex flex-col">
        {/* Top bar */}
        <div className="h-11 bg-white border-b border-gray-200 flex items-center px-4 gap-2 shadow-sm">
          <div className="flex items-center gap-1">
            <button onClick={handleNew} className="p-1.5 rounded-lg hover:bg-gray-100 transition-colors" title="新建"><Plus size={15} className="text-gray-400"/></button>
            <button onClick={() => setShowProjects(!showProjects)} className="p-1.5 rounded-lg hover:bg-gray-100 transition-colors" title="项目列表"><FolderOpen size={15} className="text-gray-400"/></button>
            <button onClick={handleSave} className="p-1.5 rounded-lg hover:bg-gray-100 transition-colors" title="保存"><Save size={15} className={`${dirty ? 'text-[#1a56db]' : 'text-gray-400'}`}/></button>
          </div>
          {currentId && <input value={projectName} onChange={e => { setProjectName(e.target.value); markDirty() }} className="text-xs border-none outline-none bg-transparent w-40 font-medium text-gray-700" placeholder="项目名称" />}
          {dirty && <span className="text-[10px] text-amber-500 font-medium">● 未保存</span>}
          <div className="flex-1" />
          <span className="text-[10px] text-gray-400 bg-gray-50 px-2 py-1 rounded-md">{entities.length} 主体 · {relations.length} 关系</span>
          <button onClick={exportPNG} className="px-3.5 py-1.5 bg-[#1a56db] text-white text-xs font-semibold rounded-lg flex items-center gap-1.5 hover:bg-[#1e40af] transition-colors shadow-sm"><Download size={13}/> 导出 PNG</button>
        </div>

        {/* Projects dropdown */}
        {showProjects && <div className="bg-white border-b max-h-48 overflow-y-auto shadow-sm">
          {projects.map(p => (
            <div key={p.id} onClick={() => { handleLoad(p.id); setShowProjects(false) }} className={`flex items-center justify-between px-4 py-2 text-xs cursor-pointer hover:bg-blue-50 ${currentId === p.id ? 'bg-blue-50' : ''}`}>
              <span>{p.name}</span>
              <div className="flex items-center gap-2">
                <span className="text-gray-400 text-[10px]">{new Date(p.updated_at).toLocaleString('zh-CN')}</span>
                <button onClick={(e) => handleDelete(p.id, e)} className="text-gray-300 hover:text-red-400"><Trash2 size={11}/></button>
              </div>
            </div>
          ))}
          {projects.length === 0 && <div className="px-4 py-3 text-xs text-gray-400 text-center">暂无保存的项目</div>}
        </div>}

        {/* Canvas */}
        <div className="flex-1" style={{ background: '#f8fafc' }}>
          <ReactFlowProvider>
            <Canvas entities={entities} relations={relations} setEntities={(fn) => { setEntities(fn); markDirty() }} setRelations={(fn) => { setRelations(fn); markDirty() }} />
          </ReactFlowProvider>
        </div>
      </div>
    </div>
  )
}
