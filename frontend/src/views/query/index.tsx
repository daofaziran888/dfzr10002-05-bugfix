import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/query'
const columns = [
  "模板编号",
  "查询模板审批状态",
  "聚合查询模板版本",
  "模板名称",
  "查询语句",
  "参数定义",
  "缓存时间",
  "结果上限",
  "执行超时"
]
const actions = [
  "提交审批",
  "撤回审批",
  "审批通过",
  "发布模板",
  "停用模板"
]
const statuses = [
  "草稿",
  "待审批",
  "已发布",
  "已停用"
]
const stats = [
  {
    "label": "查询模板总数",
    "value": 0
  },
  {
    "label": "已发布模板",
    "value": 0
  },
  {
    "label": "待审批模板",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type ConsumerSummary = {
  pendingServices: string[]
  pendingCaches: string[]
}

export default function QueryView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [consumers, setConsumers] = useState<Record<string, ConsumerSummary>>({})
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => [
    { field: 'keyword', label: '模板编号' },
    { field: 'status', label: '审批状态' },
  ], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('查询模板列表读取失败')
      }
      const payload = await response.json()
      const items: Row[] = payload.items ?? []
      setRows(items)
      setTotal(payload.total ?? 0)
      const summaries: Record<string, ConsumerSummary> = {}
      await Promise.all(items.map(async (item) => {
        const summary = await request(`${ENDPOINT}/${item.id}/consumers`)
        if (!summary.ok) {
          return
        }
        const body = await summary.json()
        summaries[String(item.id)] = {
          pendingServices: body.待重新发布数据服务 ?? [],
          pendingCaches: body.待失效缓存规则 ?? [],
        }
      }))
      setConsumers(summaries)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚合查询配置列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action, operator: '值班管理员' } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '聚合查询配置动作未生效，请稍后重试')
      }
      setNoticeMessage(payload.message ?? '')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚合查询配置操作失败')
      await reload()
    }
  }

  return (
    <section className="page" data-module="query">
      <header className="page-head">
        <div>
          <h2>聚合查询配置管理</h2>
          <p className="page-desc">维护预定义的聚合查询模板、参数化查询与缓存策略，提升分析效率。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('查询模板登记入口尚未接入审批流')}>
            登记查询模板
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出聚合查询配置清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((item) => (
          <label key={item.field} className="filter-item">
            <span>{item.label}</span>
            {item.field === 'status' ? (
              <select
                value={filters[item.field] ?? ''}
                onChange={(event) => setFilters((prev) => ({ ...prev, [item.field]: event.target.value }))}
              >
                <option value="">全部</option>
                {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
              </select>
            ) : (
              <input
                value={filters[item.field] ?? ''}
                placeholder={`按${item.label}检索`}
                onChange={(event) => setFilters((prev) => ({ ...prev, [item.field]: event.target.value }))}
              />
            )}
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>下游消费</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const summary = consumers[String(row.id)]
            const pending = (summary?.pendingServices.length ?? 0) + (summary?.pendingCaches.length ?? 0)
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
                <td>
                  {pending > 0
                    ? `待重新发布 ${summary!.pendingServices.length} 个服务，待失效 ${summary!.pendingCaches.length} 条缓存`
                    : '已同步'}
                </td>
                <td className="row-actions">
                  {actions.map((action) => (
                    <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                      {action}
                    </button>
                  ))}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无聚合查询配置数据，可先登记查询模板</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条聚合查询配置记录</span>
        {noticeMessage && <span className="notice-text">{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
