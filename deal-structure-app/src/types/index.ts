export type EntityType = 'party' | 'asset' | 'guarantee' | 'special'
export type RelationType = 'transfer' | 'guarantee' | 'payment' | 'control'
export interface Entity { 
  id: string; name: string; role: string; type: EntityType;
  borderColor?: string  // 可选自定义边框颜色
  position?: { x: number; y: number }  // 可选固定位置
}
export interface Relation { from: string; to: string; label: string; type: RelationType }
export interface Project { id: string; name: string; created_at: string; updated_at: string }
export interface ProjectDetail extends Project { entities: Entity[]; relations: Relation[] }
export const TYPE_CONFIG = {
  party: { color: '#1a56db', bg: 'linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%)', icon: '🏢', label: '交易主体' },
  asset: { color: '#b91c1c', bg: 'linear-gradient(135deg, #fef2f2 0%, #fee2e2 100%)', icon: '💎', label: '底层资产' },
  guarantee: { color: '#047857', bg: 'linear-gradient(135deg, #ecfdf5 0%, #d1fae5 100%)', icon: '🛡️', label: '担保方' },
  special: { color: '#7c3aed', bg: 'linear-gradient(135deg, #f5f3ff 0%, #ede9fe 100%)', icon: '⚙️', label: '特殊角色' },
}
export const EDGE_COLORS: Record<RelationType, string> = { transfer: '#1a56db', guarantee: '#047857', payment: '#ea580c', control: '#7c3aed' }
export const NODE_ACCENT_COLORS: Record<EntityType, string> = { party: '#1a56db', asset: '#b91c1c', guarantee: '#047857', special: '#7c3aed' }
