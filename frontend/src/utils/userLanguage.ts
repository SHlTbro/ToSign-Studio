/** Display only: protocol codes and original evidence remain unchanged. */
export function userText(value: unknown): string {
  const raw = String(value ?? '').trim()
  if (!raw) return '暂无信息'
  if (/database is locked/i.test(raw)) return '上次生成的记录保存遇到数据库占用，未完成的任务和日志已保留，可以恢复此项。'
  if (/No such file or directory/i.test(raw)) {
    if (/m2_inference\.json/.test(raw)) return '生成能力尚未完成实测，请先运行评估。'
    if (/m2_visual_review\.json/.test(raw)) return '尚未完成自动视觉审核，请在效果评估完成后继续审核。'
    if (/eval_receipt\.json/.test(raw)) return '尚未生成效果评估，请先运行评估。'
    return '所需文件缺失，请联系维护人员核对；原始记录已保留。'
  }
  if (/Real training, inference and hash gates required/i.test(raw)) return '真实训练记录、生成能力实测与模型文件校验尚未全部就绪，请先运行评估并核对结果。'
  if (/admin role required/i.test(raw)) return '此操作需要管理员权限，请使用有权限的账号。'
  if (/current artifact.*layout qualification/i.test(raw)) return '当前模型尚未完成自然排版资格核验；原有排版和已选参数保持不变。'
  if (/Natural profile not qualified/i.test(raw)) return '自然排版尚未完成资格核验。'
  if (/runtime unavailable/i.test(raw)) return '生成运行环境当前不可用，请核对正式执行资格后重新检查。'
  if (/exact positive author\/profile artifact identities required/i.test(raw)) return '来源墨色缺少已核验的作者、字体或模型身份，当前不可用。'
  if (/Independent photo QA incomplete/i.test(raw)) return '训练原图尚未完成独立质量审核，来源墨色暂不可用。'
  const known: Record<string, string> = {"UNAUTHORIZED": "登录已过期，请重新登录。", "FORBIDDEN": "当前账号没有此操作权限。", "PASSWORD_CHANGE_REQUIRED": "请先修改密码后继续。", "HASH_REQUIRED": "模型文件校验尚未完成，请先核对文件。", "OWNER_PREVIEW_STALE": "当前效果预览已变化，请刷新图片后重新确认。", "WORKER_UNAVAILABLE": "处理服务暂不可用，请稍后刷新核对。", "EXTRACT_FAILED": "字样提取失败，请核对图片后重试。", "REVIEW_READ_FAILED": "审核记录读取失败，请刷新核对。", "LIST_UNAVAILABLE": "任务列表读取失败，请刷新核对已有任务。", "E_OCCURRENCE_OVERLAP": "字样位置存在重叠，请检查框选位置。", "REVIEW_INGRESS_BUSY": "审核导入正在处理，请等待完成后刷新。", "M2_RUNTIME_UNAVAILABLE": "训练环境暂不可用，请检查运行环境。", "ARTIFACT_INVALID": "模型文件校验未通过，请联系维护人员核对。", "RUN_NOT_FOUND": "找不到此训练记录，或当前账号无权访问。", "SNAPSHOT_MISMATCH": "训练材料与已冻结记录不一致，请重新核对材料。", "SAMPLE_ALREADY_REVIEWED": "此字样已审核，请刷新读取最新审核结果。", "RECOVERY_UNAVAILABLE": "训练恢复状态暂不可用，请刷新核对。", "E_RUNTIME_REGISTRATION_MISSING": "原执行环境登记不存在，请联系维护人员恢复正确环境。", "E_RUNTIME_CHANGED": "执行环境已变化，请重新核对所选环境。", "STALE_CACHE": "训练缓存已过期，请重新准备缓存。", "ZIP_TOO_LARGE": "打包文件过大，请减少本次导出数量。", "NEED_VALIDATION_CHARACTERS": "验证组还需补充合格字符，请检查分组。", "INSUFFICIENT_CONFIRMED_CHARACTERS": "已确认的字符数量不足，请补充并审核字样。", "TEMPLATE_NOT_FOUND": "找不到此布局模板，或当前账号无权访问。", "REVIEW_INGRESS_CHANGED": "审核导入记录已变化，请重新核验。", "OWNERSHIP_UNKNOWN": "任务归属尚未确认，请刷新核对。"}
  const code = Object.keys(known).find(key => new RegExp(`\\b${key}\\b`).test(raw))
  if (code === 'ZIP_TOO_LARGE') {
    const sizes = raw.match(/payload is (\d+) bytes, cap is (\d+)/i)
    if (sizes) return `打包文件过大：预计 ${sizes[1]} 字节，允许上限 ${sizes[2]} 字节。请减少本次导出数量。`
  }
  if (code) return known[code]
  if (/Template not found/i.test(raw)) return '找不到此布局模板，或当前账号无权访问。'
  if (/Missing current occurrence selection/i.test(raw)) return '尚未选择当前采用的字样版本，请先完成字样选择。'
  const messages: string[] = []
  const staleReview = /REVIEW_JOB_STALE|candidate review implementation changed/i.test(raw)
  if (staleReview) messages.push('已有字样审核记录已过期，需要按当前规则重新审核。')
  const count = raw.match(/Need (\d+) more distinct eligible characters/i)
  if (count) messages.push(staleReview ? `当前还需 ${count[1]} 个不同字样通过审核，可先重新审核已有字样。` : `新训练还需补充 ${count[1]} 个不同的合格字符。`)
  if (/NEED_NEW_INPUTS/.test(raw) && !count) messages.push('新训练还需补充合格字样。')
  if (/E_WINDOWS_GENERIC_QUALIFICATION_REQUIRED/.test(raw)) messages.push('所选新训练环境尚未完成独立资格验证，请联系维护人员。')
  if (/E_RUNTIME_BLOCKED/.test(raw)) messages.push('所选执行环境尚未就绪，请检查环境状态。')
  if (/EVAL_NOT_PRODUCED/.test(raw)) messages.push('尚未生成效果评估；请先运行评估。')
  if (/AI_PROVIDER_UNAVAILABLE/.test(raw)) messages.push('自动视觉审核服务尚未就绪。')
  if (/STYLE_NOT_FOUND/.test(raw)) messages.push('找不到此字体，或当前账号无权访问。')
  if (/TIMEOUT/.test(raw)) messages.push('请求超时，请刷新核对结果后再操作。')
  if (/transport failure|transport message/i.test(raw)) messages.push('网络通信失败，请刷新核对任务结果。')
  if (/scope mismatch/i.test(raw)) messages.push('操作范围与当前任务不一致，请重新核对。')
  if (/ML runtime missing|capability absent/i.test(raw)) messages.push('训练环境缺少所需能力，请检查环境。')
  if (/file changed after earlier check/i.test(raw)) messages.push('文件在上次检查后发生变化，请重新核验。')
  if (/second style blocked/i.test(raw)) messages.push('所选字体的训练环境尚未就绪。')
  if (messages.length) return [...new Set(messages)].join(' ')
  // Preserve Chinese explanations while keeping internal codes out of normal views.
  if (/[\u3400-\u9fff]/.test(raw)) return raw.replace(/^Error:\s*/, '').replace(/^(?:\d{3}\s+)?(?:[A-Z][A-Z0-9_]+[：:]\s*)+/, '').replace(/\b(?:E_)?[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b[：: ]*/g, '').replace(/\bHTTP\s*/g, '').trim()
  return '操作暂未完成，请刷新核对状态；仍失败时请联系维护人员。'
}

export function userError(error: { code?: unknown; message?: unknown }): string {
  return userText(`${error.code ?? ''}：${error.message ?? ''}`)
}

const states: Record<string, string> = {
  uploaded: '已登记',
  M2: '个人手写模型', M2_ADAPTER: '个人手写模型', M1: '预置字形模型', M0: '系统字体', ready: '已就绪', READY: '已就绪', BLOCKED: '待解决', blocked: '待解决', PENDING: '待完成', pending: '待完成',
  PASS: '通过', PASSED: '通过', FAIL: '未通过', FAILED: '未通过', failed: '失败', running: '进行中', queued: '排队中',
  succeeded: '已完成', complete: '已完成', not_started: '未开始', unavailable: '暂无结果', available: '可查看',
  approved: '已审核', draft: '草稿', pending_review: '待审核', rejected: '已退回', cancelled: '已取消', created: '已创建',
  PUBLISH_READY_PENDING_OWNER: '等待本人验收', PUBLISH_READY_AI_AUTHORIZED: '已满足发布条件',
  WAIVED_BY_USER: '本人已明确豁免', OWNER_REQUIRED: '需本人验收', NOT_RECORDED: '尚未记录', ACCEPTED: '已接受',
  admitted: '已入库', training_complete: '训练完成', admitted_complete: '已入库', confirmed: '已确认', confirm: '确认', reject: '排除', isolated: '待复核', ADMITTED: '已入库', HANDWRITING_ASSET: '手写素材', PRESET: '预设字体',
  OWNERSHIP_UNKNOWN: '任务归属尚未确认，请刷新核对。', UNKNOWN: '状态未知', AI_AUTHORIZED: '已授权自动审核', PERSONAL_NONCOMMERCIAL: '个人非商业使用', REQUIRED: '需完成', training: '训练', inference: '生成能力', ai_visual: '自动视觉审核', hash_verification: '文件校验', evaluation: '效果评估',
}
export function stateLabel(value: unknown): string { return states[String(value)] ?? (/[\u3400-\u9fff]/.test(String(value ?? '')) ? userText(value) : '状态待确认') }
