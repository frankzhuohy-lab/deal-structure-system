import { Entity, Relation } from '../types'
export interface Template { name: string; entities: Entity[]; relations: Relation[] }
export const templates: Template[] = [
  { name: '应收账款保理', entities: [
    { id:'1', name:'中鹏保理', role:'融资方/转让方', type:'party' }, { id:'2', name:'皖信投资', role:'受让方/投资人', type:'party' },
    { id:'3', name:'宁国国投', role:'核心债务人', type:'party' }, { id:'4', name:'应收账款收益权', role:'底层资产 ¥2,880万', type:'asset' },
    { id:'5', name:'宁国国控', role:'担保方（连带）', type:'guarantee' }, { id:'6', name:'宁阳控股', role:'担保方（连带）', type:'guarantee' },
  ], relations: [
    { from:'1', to:'2', label:'转让收益权', type:'transfer' }, { from:'2', to:'1', label:'支付对价 ¥2,850万', type:'payment' },
    { from:'1', to:'4', label:'底层资产', type:'transfer' }, { from:'3', to:'4', label:'债务人', type:'payment' },
    { from:'5', to:'2', label:'连带担保', type:'guarantee' }, { from:'6', to:'2', label:'连带担保', type:'guarantee' },
    { from:'3', to:'1', label:'到期兑付', type:'payment' },
  ]},
  { name: '信托贷款结构', entities: [
    { id:'1', name:'XX信托计划', role:'放款主体', type:'party' }, { id:'2', name:'融资方A公司', role:'借款方', type:'party' },
    { id:'3', name:'B银行', role:'资金托管行', type:'special' }, { id:'4', name:'C大厦', role:'抵押物', type:'asset' },
    { id:'5', name:'D担保公司', role:'保证担保人', type:'guarantee' },
  ], relations: [
    { from:'1', to:'2', label:'发放贷款', type:'payment' }, { from:'2', to:'1', label:'还本付息', type:'payment' },
    { from:'3', to:'1', label:'资金监管', type:'control' }, { from:'2', to:'4', label:'资产抵押', type:'transfer' },
    { from:'5', to:'1', label:'保证担保', type:'guarantee' },
  ]},
  { name: '有限合伙基金', entities: [
    { id:'1', name:'LP 有限合伙人', role:'出资 99%', type:'party' }, { id:'2', name:'GP 普通合伙人', role:'管理人', type:'party' },
    { id:'3', name:'有限合伙基金', role:'投资载体', type:'party' }, { id:'4', name:'被投企业X', role:'标的公司', type:'party' },
  ], relations: [
    { from:'1', to:'3', label:'出资 99%', type:'payment' }, { from:'2', to:'3', label:'出资 1% + 管理', type:'control' },
    { from:'3', to:'4', label:'股权投资', type:'transfer' }, { from:'4', to:'3', label:'收益分配', type:'payment' },
    { from:'3', to:'2', label:'管理费+Carry', type:'payment' }, { from:'3', to:'1', label:'收益分配', type:'payment' },
  ]},
]
