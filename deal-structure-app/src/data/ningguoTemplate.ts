import { Entity, Relation, EntityType } from '../types'

// 宁国交易结构图 - 完全按照设计图
export const NINGGUO_TEMPLATE = {
  name: '宁国交易结构图',
  entities: [
    {
      id: '1',
      name: '中鹏保理',
      role: '融资方/转让方',
      type: 'party' as EntityType,
      borderColor: '#f97316',  // 橙色
      position: { x: 50, y: 50 },
    },
    {
      id: '2',
      name: '皖信投资',
      role: '受让方/投资人',
      type: 'party' as EntityType,
      borderColor: '#3b82f6',  // 蓝色
      position: { x: 500, y: 50 },
    },
    {
      id: '3',
      name: '应收账款收益权',
      role: '底层资产 (金额: 2,880万)',
      type: 'asset' as EntityType,
      borderColor: '#a855f7',  // 紫色
      position: { x: 50, y: 300 },
    },
    {
      id: '4',
      name: '宁国国控',
      role: '担保方1',
      type: 'guarantee' as EntityType,
      borderColor: '#f97316',  // 橙色
      position: { x: 300, y: 300 },
    },
    {
      id: '5',
      name: '宁国国投',
      role: '核心债务人',
      type: 'party' as EntityType,
      borderColor: '#ef4444',  // 红色
      position: { x: 550, y: 300 },
    },
    {
      id: '6',
      name: '宁阳控股',
      role: '担保方2',
      type: 'guarantee' as EntityType,
      borderColor: '#06b6d4',  // 浅蓝色
      position: { x: 750, y: 300 },
    },
  ] as (Entity & { position: { x: number; y: number } })[],
  relations: [
    {
      from: '1',
      to: '2',
      label: '应收账款收益权转让',
      type: 'transfer' as const,
      color: '#f97316',  // 橙色
    },
    {
      from: '2',
      to: '1',
      label: '投资款/受让价款',
      type: 'payment' as const,
      color: '#22c55e',  // 绿色
      dashed: true,
    },
    {
      from: '1',
      to: '3',
      label: '底层资产',
      type: 'transfer' as const,
      color: '#a855f7',  // 紫色
    },
    {
      from: '3',
      to: '5',
      label: '到期付款/兑付',
      type: 'payment' as const,
      color: '#ef4444',  // 红色
    },
    {
      from: '4',
      to: '1',
      label: '连带责任担保',
      type: 'guarantee' as const,
      color: '#f97316',  // 橙色
    },
    {
      from: '6',
      to: '2',
      label: '连带责任担保',
      type: 'guarantee' as const,
      color: '#06b6d4',  // 浅蓝色
    },
  ] as (Relation & { color?: string; dashed?: boolean })[],
}
