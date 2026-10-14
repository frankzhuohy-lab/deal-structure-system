import { Entity, Relation } from '../types'

const KEYWORDS: { keywords: string[]; type: Entity['type']; role: string; borderColor?: string }[] = [
  { keywords: ['融资方','转让方','卖方','发起方'], type: 'party', role: '融资方/转让方', borderColor: '#f97316' },
  { keywords: ['受让方','买方','投资人','投资方','收购方'], type: 'party', role: '受让方/投资人', borderColor: '#3b82f6' },
  { keywords: ['债务人','核心企业','被投企业','标的','付款方'], type: 'party', role: '核心债务人', borderColor: '#ef4444' },
  { keywords: ['担保方','保证人','担保人','增信方'], type: 'guarantee', role: '担保方', borderColor: '#f97316' },
  { keywords: ['底层资产','资产','抵押物','质押物','收益权'], type: 'asset', role: '底层资产', borderColor: '#a855f7' },
  { keywords: ['托管','监管','托管方','监管方'], type: 'special', role: '托管方/监管方', borderColor: '#06b6d4' },
  { keywords: ['管理人','GP','基金管理人'], type: 'party', role: '基金管理人', borderColor: '#8b5cf6' },
  { keywords: ['LP','出资方','出资人','劣后级'], type: 'party', role: '出资方', borderColor: '#6366f1' },
  { keywords: ['财务顾问','FA','顾问'], type: 'special', role: '财务顾问', borderColor: '#8b5cf6' },
  { keywords: ['律所','律师','法律顾问'], type: 'special', role: '法律顾问', borderColor: '#8b5cf6' },
  { keywords: ['银行','贷款方','授信方','资金方'], type: 'party', role: '资金方/银行', borderColor: '#2563eb' },
]

export function parseText(text: string): { entities: Entity[]; relations: Relation[] } {
  const entities: Entity[] = []; let id = 1
  const segments: string[] = []
  for (const line of text.split('\n').filter(l => l.trim())) {
    if (/[：:]/.test(line) && /[，,]/.test(line)) {
      let buf = ''
      for (const ch of line) {
        if (ch === '，' || ch === ',') {
          if (buf.trim()) segments.push(buf.trim())
          buf = ''
        } else {
          buf += ch
        }
      }
      if (buf.trim()) segments.push(buf.trim())
    } else {
      segments.push(line.trim())
    }
  }

  const roleCount: Record<string, number> = {}
  for (const seg of segments) {
    const m = seg.match(/(.+?)[：:]\s*(.+)/)
    if (!m) continue
    const key = m[1].trim()
    const value = m[2].trim()
    if (!value) continue
    for (const kw of KEYWORDS) {
      if (kw.keywords.some(k => key.includes(k))) {
        const baseRole = kw.role
        roleCount[baseRole] = (roleCount[baseRole] || 0) + 1
        const role = roleCount[baseRole] > 1 ? `${baseRole}${roleCount[baseRole]}` : baseRole
        entities.push({ 
          id: String(id++), 
          name: value, 
          role, 
          type: kw.type,
          borderColor: kw.borderColor 
        })
        break
      }
    }
  }
  return { entities, relations: autoGen(entities) }
}

function autoGen(es: Entity[]): Relation[] {
  const rs: Relation[] = []
  const parties = es.filter(e => e.type === 'party')
  const assets = es.filter(e => e.type === 'asset')
  const guarantees = es.filter(e => e.type === 'guarantee')

  // 只生成核心关系，避免多余的线
  // 1. 融资方 → 受让方（交易）
  if (parties.length >= 2) {
    rs.push({ from: parties[0].id, to: parties[1].id, label: '交易', type: 'transfer' })
  }
  // 2. 债务人 → 融资方（还款）
  if (parties.length >= 3) {
    rs.push({ from: parties[2].id, to: parties[0].id, label: '还款', type: 'payment' })
  }
  // 3. 融资方 → 资产（底层资产）
  if (parties.length && assets.length) {
    rs.push({ from: parties[0].id, to: assets[0].id, label: '资产', type: 'transfer' })
  }
  // 4. 担保方 → 融资方（担保）
  for (const g of guarantees) {
    if (parties.length) {
      rs.push({ from: g.id, to: parties[0].id, label: '担保', type: 'guarantee' })
    }
  }
  return rs
}

// ============ 分层布局（减少连线交叉）============

const NW = 200   // 节点宽
const NH = 90    // 节点高
const GAP_X = 100 // 水平间距
const GAP_Y = 80  // 垂直间距

export function autoRelayout(
  entities: Entity[],
  relations?: Relation[]
): { id: string; position: { x: number; y: number } }[] {
  const n = entities.length
  if (n === 0) return []
  if (n === 1) return [{ id: entities[0].id, position: { x: 300, y: 200 } }]

  // 按角色分组
  const groups = {
    originator: entities.filter(e => e.role.includes('融资') || e.role.includes('转让') || e.role.includes('发起')),
    investor: entities.filter(e => e.role.includes('受让') || e.role.includes('投资') || e.role.includes('买方')),
    debtor: entities.filter(e => e.role.includes('债务') || e.role.includes('核心') || e.role.includes('标的')),
    guarantee: entities.filter(e => e.type === 'guarantee'),
    asset: entities.filter(e => e.type === 'asset'),
    other: entities.filter(e => 
      !e.role.includes('融资') && !e.role.includes('转让') && !e.role.includes('发起') &&
      !e.role.includes('受让') && !e.role.includes('投资') && !e.role.includes('买方') &&
      !e.role.includes('债务') && !e.role.includes('核心') && !e.role.includes('标的') &&
      e.type !== 'guarantee' && e.type !== 'asset'
    ),
  }

  // 分层布局：左→中→右
  // 左侧：融资方/发起方
  // 中间：资产、其他
  // 右侧：受让方/投资人、债务人
  // 下方：担保方

  const layers: Entity[][] = []
  
  // 左侧列
  if (groups.originator.length > 0) layers.push(groups.originator)
  
  // 中间列（资产、其他）
  const middle = [...groups.asset, ...groups.other]
  if (middle.length > 0) layers.push(middle)
  
  // 右侧列（受让方、债务人）
  const right = [...groups.investor, ...groups.debtor]
  if (right.length > 0) layers.push(right)

  // 下方行（担保方）
  const hasBottom = groups.guarantee.length > 0

  // 计算位置
  const positions: { id: string; position: { x: number; y: number } }[] = []
  
  // 计算每列的最大节点数
  const maxColHeight = Math.max(...layers.map(l => l.length), 1)
  const totalHeight = maxColHeight * NH + (maxColHeight - 1) * GAP_Y

  // 布局左侧列
  let colX = 0
  for (let col = 0; col < layers.length; col++) {
    const layer = layers[col]
    const layerHeight = layer.length * NH + (layer.length - 1) * GAP_Y
    const startY = (totalHeight - layerHeight) / 2

    for (let row = 0; row < layer.length; row++) {
      positions.push({
        id: layer[row].id,
        position: {
          x: colX,
          y: startY + row * (NH + GAP_Y),
        },
      })
    }
    colX += NW + GAP_X
  }

  // 布局下方行（担保方）
  if (hasBottom) {
    const bottomY = totalHeight + GAP_Y
    const bottomWidth = groups.guarantee.length * NW + (groups.guarantee.length - 1) * GAP_X
    const startX = (colX - GAP_X - bottomWidth) / 2

    for (let i = 0; i < groups.guarantee.length; i++) {
      positions.push({
        id: groups.guarantee[i].id,
        position: {
          x: startX + i * (NW + GAP_X),
          y: bottomY,
        },
      })
    }
  }

  return positions
}
