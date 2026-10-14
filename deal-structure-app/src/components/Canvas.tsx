import { useMemo, useCallback } from 'react'
import { ReactFlow, Background, Controls, MiniMap, MarkerType, applyNodeChanges } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import CustomNode from './CustomNode'
import { Entity, Relation, EDGE_COLORS } from '../types'
import { autoRelayout } from '../utils/aiParser'

const nodeTypes = { custom: CustomNode }

interface Props {
  entities: Entity[]; relations: Relation[]
  setEntities: (fn: (prev: Entity[]) => Entity[]) => void
  setRelations: (fn: (prev: Relation[]) => Relation[]) => void
}

// 扩展 Relation 支持自定义颜色和虚线
interface ExtendedRelation extends Relation {
  color?: string
  dashed?: boolean
}

export default function Canvas({ entities, relations, setEntities, setRelations }: Props) {
  const { nodes, edges } = useMemo(() => {
    const positions = autoRelayout(entities, relations)
    return {
      nodes: entities.map(e => ({
        id: e.id, type: 'custom' as const,
        // 优先使用实体自带的位置，否则用自动布局
        position: e.position || positions.find(p => p.id === e.id)?.position || { x: 100, y: 100 },
        data: { name: e.name, role: e.role, type: e.type, borderColor: e.borderColor },
      })),
      edges: (relations as ExtendedRelation[]).map((r, i) => {
        const edgeColor = r.color || EDGE_COLORS[r.type]
        return {
          id: `e${i}`,
          source: r.from,
          target: r.to,
          label: r.label,
          type: 'straight',
          markerEnd: { type: MarkerType.ArrowClosed as any, color: edgeColor, width: 20, height: 20 },
          style: {
            stroke: edgeColor,
            strokeWidth: 2,
            strokeDasharray: r.dashed ? '8,4' : undefined,
          },
          labelStyle: { fill: edgeColor, fontSize: 10, fontWeight: 700, fontFamily: 'system-ui' },
          labelBgStyle: { fill: '#ffffff', fillOpacity: 0.95 },
          labelBgPadding: [8, 4] as any,
          labelBgBorderRadius: 4,
        }
      }),
    }
  }, [entities, relations])

  const onNodesChange = useCallback((changes: any) => {
    setEntities(prev => {
      // 创建包含当前位置的节点用于 applyNodeChanges
      const nodesWithPositions = prev.map(e => ({
        id: e.id, 
        type: 'custom' as const,
        position: e.position || { x: 0, y: 0 },
        data: { name: e.name, role: e.role, type: e.type, borderColor: e.borderColor },
      }))
      const updated = applyNodeChanges(changes, nodesWithPositions)
      return prev.map(e => {
        const node = updated.find((n: any) => n.id === e.id)
        return node ? { ...e, position: node.position } : e
      })
    })
  }, [setEntities])

  return (
    <div className="w-full h-full">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.3 }}
        attributionPosition="bottom-left"
      >
        <Background color="#e2e8f0" gap={24} size={1} />
        <Controls showInteractive={false} className="rounded-xl shadow-lg border border-gray-200" />
        <MiniMap nodeStrokeColor="#cbd5e1" nodeColor="#f8fafc" maskColor="rgba(0,0,0,0.04)" style={{ border: '1px solid #e2e8f0', borderRadius: 12 }} />
      </ReactFlow>
    </div>
  )
}
