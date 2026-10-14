import { Handle, Position } from '@xyflow/react'
import { TYPE_CONFIG, NODE_ACCENT_COLORS, EntityType } from '../types'

export default function CustomNode({ data }: { data: any }) {
  const entityType = (data.type || 'party') as EntityType
  const cfg = TYPE_CONFIG[entityType] || TYPE_CONFIG.party
  // 优先使用自定义边框颜色
  const accent = data.borderColor || NODE_ACCENT_COLORS[entityType] || '#1a56db'
  
  return (
    <div 
      className="relative rounded-xl shadow-md border-0 bg-white" 
      style={{ 
        minWidth: 160,
        boxShadow: `0 4px 20px rgba(0,0,0,0.08), 0 1px 3px rgba(0,0,0,0.05)`,
        borderLeft: `4px solid ${accent}`,
        borderTop: `2px solid ${accent}`,
        borderRight: `2px solid ${accent}`,
        borderBottom: `2px solid ${accent}`,
      }}
    >
      <Handle 
        type="target" 
        position={Position.Top} 
        style={{ 
          background: accent, 
          width: 10, 
          height: 10, 
          border: '3px solid white',
          boxShadow: '0 0 0 2px ' + accent + '40',
        }} 
      />
      
      {/* Header bar */}
      <div className="flex items-center gap-2.5 px-4 py-3 rounded-tr-xl" style={{ background: accent + '08' }}>
        <div 
          className="flex items-center justify-center w-9 h-9 rounded-lg flex-shrink-0"
          style={{ background: accent + '15' }}
        >
          <span className="text-base">{cfg.icon}</span>
        </div>
        <div className="min-w-0">
          <div className="text-[13px] font-bold leading-tight truncate" style={{ color: '#1e293b' }}>
            {data.name}
          </div>
          <div className="text-[11px] leading-tight mt-0.5" style={{ color: accent }}>
            {data.role || cfg.label}
          </div>
        </div>
      </div>

      {/* Type badge */}
      <div className="px-4 pb-3 pt-0 flex items-center gap-1.5">
        <span 
          className="text-[10px] font-semibold px-2 py-0.5 rounded-full"
          style={{ background: accent + '12', color: accent }}
        >
          {cfg.label}
        </span>
      </div>

      <Handle 
        type="source" 
        position={Position.Bottom} 
        style={{ 
          background: accent, 
          width: 10, 
          height: 10, 
          border: '3px solid white',
          boxShadow: '0 0 0 2px ' + accent + '40',
        }} 
      />
    </div>
  )
}
