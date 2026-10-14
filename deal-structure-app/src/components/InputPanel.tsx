import { useState } from 'react'
import { Plus, Trash2, Sparkles, Loader2 } from 'lucide-react'
import { Entity, Relation, EntityType, RelationType, TYPE_CONFIG, EDGE_COLORS } from '../types'
import { parseText } from '../utils/aiParser'
import { aiParse } from '../api'
import { templates } from '../utils/templates'

interface Props {
  entities: Entity[]; setEntities: (e: Entity[] | ((p: Entity[]) => Entity[])) => void
  relations: Relation[]; setRelations: (r: Relation[] | ((p: Relation[]) => Relation[])) => void
  activeTab: string; setActiveTab: (t: string) => void
}

export default function InputPanel({ entities, setEntities, relations, setRelations }: Props) {
  const [tab, setTab] = useState('ai')
  const [aiText, setAiText] = useState('')
  const [aiLoading, setAiLoading] = useState(false)
  const [aiError, setAiError] = useState('')
  const [nm, setNm] = useState(''); const [rl, setRl] = useState(''); const [tp, setTp] = useState<EntityType>('party')
  const [rf, setRf] = useState(''); const [rt, setRt] = useState(''); const [rLab, setRLab] = useState(''); const [rTyp, setRTyp] = useState<RelationType>('transfer')
  const get = (id: string) => entities.find(e => e.id === id)?.name || id

  const doParse = async () => {
    if (!aiText.trim()) return
    setAiLoading(true); setAiError('')
    try {
      const r = await aiParse(aiText)
      if (r.entities.length) {
        setEntities(r.entities)
        setRelations(r.relations)
      } else {
        const local = parseText(aiText)
        if (local.entities.length) {
          setEntities(local.entities); setRelations(local.relations)
          setAiError('AI未识别到实体，已使用本地解析')
        }
      }
    } catch (err: any) {
      setAiError(err.message || 'AI解析失败')
      const local = parseText(aiText)
      if (local.entities.length) {
        setEntities(local.entities); setRelations(local.relations)
      }
    } finally {
      setAiLoading(false)
    }
  }
  const addEntity = () => { if (nm) { setEntities(p => [...p, { id: String(Date.now()), name: nm, role: rl, type: tp }]); setNm('') } }
  const addRelation = () => { if (rf && rt) { setRelations((p: Relation[]) => [...p, { from: rf, to: rt, label: rLab, type: rTyp }]); setRLab('') } }
  const rmEntity = (id: string) => { setEntities((p: Entity[]) => p.filter(e => e.id !== id)); setRelations((p: Relation[]) => p.filter(r => r.from !== id && r.to !== id)) }
  const rmRelation = (i: number) => setRelations((p: Relation[]) => p.filter((_, idx) => idx !== i))

  return (
    <div className="w-80 bg-white border-r border-gray-200 flex flex-col shadow-sm">
      <div className="px-4 py-3.5 bg-gradient-to-r from-[#1e3a5f] via-[#1a56db] to-[#2563eb] text-white">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg flex items-center justify-center bg-white/15 text-lg">📊</div>
          <div>
            <div className="font-bold text-sm">交易结构图生成器</div>
            <div className="text-[10px] opacity-70">AI-Powered Deal Structure</div>
          </div>
        </div>
      </div>
      <div className="flex border-b bg-gray-50">
        {[{ k: 'ai', l: '🤖 AI 解析' }, { k: 'entity', l: '🏢 主体' }, { k: 'relation', l: '🔗 关系' }].map(t => (
          <button key={t.k} onClick={() => setTab(t.k)} className={`flex-1 py-2.5 text-xs font-semibold transition-colors ${tab === t.k ? 'text-[#1a56db] border-b-2 border-[#1a56db] bg-white' : 'text-gray-400 hover:text-gray-600'}`}>{t.l}</button>
        ))}
      </div>
      <div className="flex-1 overflow-y-auto p-3 space-y-3">
        {tab === 'ai' && <>
          <textarea value={aiText} onChange={e => setAiText(e.target.value)} placeholder={'描述交易结构，例如：\n中鹏保理将2,880万应收账款收益权转让给皖信投资，宁国国投作为核心债务人，宁国国控和宁阳控股提供连带担保'} className="w-full h-36 p-3 text-xs border border-gray-200 rounded-xl resize-none focus:outline-none focus:border-[#1a56db] focus:ring-2 focus:ring-blue-100 bg-gray-50" />
          <button onClick={doParse} disabled={aiLoading} className="w-full py-2.5 bg-gradient-to-r from-[#1a56db] to-[#2563eb] text-white text-xs font-bold rounded-xl flex items-center justify-center gap-1.5 disabled:opacity-60 shadow-lg shadow-blue-200 transition-all hover:shadow-xl hover:shadow-blue-300">
            {aiLoading ? <><Loader2 size={14} className="animate-spin"/> AI解析中...</> : <><Sparkles size={14}/> AI 智能解析</>}
          </button>
          {aiError && <div className="text-[10px] text-amber-600 bg-amber-50 px-3 py-1.5 rounded-lg">⚠️ {aiError}</div>}
          <div className="border-t border-gray-100 pt-3"><div className="text-xs font-bold text-gray-500 mb-2 flex items-center gap-1.5">📋 快速模板</div>
            {templates.map((t, i) => <button key={i} onClick={() => { setEntities(t.entities); setRelations(t.relations) }} className="w-full text-left p-3 rounded-xl border border-gray-100 mb-1.5 hover:border-[#1a56db] hover:bg-blue-50/30 transition-all text-xs">
              <div className="flex items-center gap-2">
                <span className="font-bold text-gray-700">{t.name}</span>
                <span className="text-[10px] text-gray-400 bg-gray-50 px-1.5 py-0.5 rounded">{t.entities.length}主体·{t.relations.length}关系</span>
              </div>
            </button>)}
          </div>
        </>}
        {tab === 'entity' && <>
          <div className="flex gap-1.5">
            <input value={nm} onChange={e => setNm(e.target.value)} placeholder="名称" className="flex-1 px-2.5 py-2 text-xs border border-gray-200 rounded-lg focus:outline-none focus:border-[#1a56db]" onKeyDown={e => e.key === 'Enter' && addEntity()} />
            <input value={rl} onChange={e => setRl(e.target.value)} placeholder="角色" className="w-20 px-2.5 py-2 text-xs border border-gray-200 rounded-lg focus:outline-none focus:border-[#1a56db]" onKeyDown={e => e.key === 'Enter' && addEntity()} />
          </div>
          <div className="flex gap-1.5">
            <select value={tp} onChange={e => setTp(e.target.value as EntityType)} className="flex-1 px-2.5 py-2 text-xs border border-gray-200 rounded-lg bg-white">
              <option value="party">🏢 交易主体</option><option value="asset">💎 资产</option><option value="guarantee">🛡️ 担保</option><option value="special">⚙️ 其他</option>
            </select>
            <button onClick={addEntity} className="px-3 py-2 bg-[#1a56db] text-white text-xs rounded-lg hover:bg-[#1e40af] transition-colors"><Plus size={14}/></button>
          </div>
          <div className="space-y-1.5">{[...entities].reverse().map(e => <div key={e.id} className="flex items-center justify-between p-2.5 rounded-xl border border-gray-100 bg-white hover:shadow-sm transition-shadow" style={{ borderLeft: `4px solid ${TYPE_CONFIG[e.type].color}` }}>
            <div><div className="text-xs font-bold text-gray-700">{e.name}</div><div className="text-[10px] text-gray-400">{e.role || TYPE_CONFIG[e.type].label}</div></div>
            <button onClick={() => rmEntity(e.id)} className="text-gray-300 hover:text-red-400 p-1"><Trash2 size={13}/></button>
          </div>)}</div>
        </>}
        {tab === 'relation' && <>
          <div className="flex gap-1.5 items-center">
            <select value={rf} onChange={e => setRf(e.target.value)} className="flex-1 px-2.5 py-2 text-xs border border-gray-200 rounded-lg bg-white"><option value="">起始主体</option>{entities.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}</select>
            <span className="text-gray-300 text-sm font-bold">→</span>
            <select value={rt} onChange={e => setRt(e.target.value)} className="flex-1 px-2.5 py-2 text-xs border border-gray-200 rounded-lg bg-white"><option value="">目标主体</option>{entities.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}</select>
          </div>
          <div className="flex gap-1.5">
            <input value={rLab} onChange={e => setRLab(e.target.value)} placeholder="关系描述" className="flex-1 px-2.5 py-2 text-xs border border-gray-200 rounded-lg" />
            <select value={rTyp} onChange={e => setRTyp(e.target.value as RelationType)} className="w-20 px-2.5 py-2 text-xs border border-gray-200 rounded-lg bg-white">
              <option value="transfer">转移</option><option value="payment">资金</option><option value="guarantee">担保</option><option value="control">控制</option>
            </select>
            <button onClick={addRelation} className="px-3 py-2 bg-[#1a56db] text-white text-xs rounded-lg hover:bg-[#1e40af] transition-colors"><Plus size={14}/></button>
          </div>
          <div className="space-y-1.5">{[...relations].reverse().map((r, i) => <div key={i} className="flex items-center justify-between p-2.5 rounded-xl border border-gray-100 bg-white hover:shadow-sm transition-shadow">
            <div className="flex items-center gap-1 text-xs min-w-0">
              <span className="font-bold text-gray-700 truncate">{get(r.from)}</span>
              <span className="text-gray-300 flex-shrink-0">→</span>
              <span className="font-bold text-gray-700 truncate">{get(r.to)}</span>
              <span className="ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold flex-shrink-0" style={{ color: EDGE_COLORS[r.type], background: EDGE_COLORS[r.type] + '12' }}>
                {r.label || r.type}
              </span>
            </div>
            <button onClick={() => rmRelation(relations.length - 1 - i)} className="text-gray-300 hover:text-red-400 p-1 flex-shrink-0"><Trash2 size={13}/></button>
          </div>)}</div>
        </>}
      </div>
    </div>
  )
}
