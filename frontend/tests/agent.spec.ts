import { test, expect, type Page } from '@playwright/test'

async function mockAgent(page: Page) {
  const sessions: any[] = [], sent: any[] = [], savedSettings: any[] = []
  let settings = { base_url: 'https://example.invalid/v1', model: 'test-model', protocol: 'responses', has_key: true, configured: true,
    timeout_seconds: 120, max_output_tokens: 4096, strict_tools: true }
  await page.route('**/api/agent/**', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname.replace('/api/agent/', ''), body = request.postDataJSON()
    let value: unknown
    if (path === 'settings') {
      if (request.method() === 'PUT') { savedSettings.push(body); settings = { ...settings, ...body } }
      value = settings
    } else if (path === 'test') value = { text: '模型工具连接正常', usage: { input_tokens: 200, output_tokens: 10, cached_tokens: 100 } }
    else if (path === 'sessions' && request.method() === 'GET') value = sessions.filter(session => session.project_id === url.searchParams.get('project_id') && session.canvas_id === url.searchParams.get('canvas_id'))
    else if (path === 'sessions' && request.method() === 'POST') {
      value = { ...body, id: `session-${sessions.length + 1}`, title: '新对话', status: 'completed', updated_at: 1, messages: [], runs: [], active_run_id: null }
      sessions.push(value)
    } else if (/^sessions\/[^/]+\/messages$/.test(path)) {
      const session = sessions.find(item => item.id === path.split('/')[1])
      sent.push(body); session.title = '灯塔的故事'; session.messages.push({ role: 'user', content: body.message })
      const run = { id: `run-${session.id}`, session_id: session.id, status: 'running', mode: body.mode, message: '正在编排', created_at: 1, updated_at: 2,
        usage: { input_tokens: 1000, output_tokens: 200, cached_tokens: 800 }, events: [{ id: 'plan', type: 'plan', status: 'completed', text: '以同一个角色串起三幕故事', created_at: 1 },
          { id: 'tool', type: 'tool', name: 'edit_canvas', status: 'completed', arguments: { node_ids: body.selected_nodes }, result: { node_count: 3, api_key: 'never-render-this-secret' }, created_at: 1 }] }
      session.runs.push(run); session.active_run_id = run.id; session.status = 'running'; value = run
    } else if (/^runs\/[^/]+$/.test(path)) {
      const session = sessions.find(item => item.runs.some((run: any) => run.id === path.split('/')[1]))
      const run = session.runs.at(-1); run.status = 'completed'; session.status = 'completed'; session.active_run_id = null
      if (!session.messages.some((message: any) => message.role === 'assistant')) session.messages.push({ role: 'assistant', content: '分镜和连线已准备好。\n\n**角色固定**\n- 使用 `黄铜机器人`\n- [本地素材](/api/assets/example/file)\n- [参考](https://example.com/story)\n- [危险](javascript:alert(1))\n\n<img src=x onerror=alert(1)>' })
      value = run
    } else if (/^sessions\/[^/]+$/.test(path)) value = sessions.find(item => item.id === path.split('/')[1])
    else throw new Error(`Unexpected mock Agent endpoint: ${request.method()} ${path}`)
    await route.fulfill({ json: value })
  })
  return { sessions, sent, savedSettings }
}

test('short Agent panels keep composer controls reachable and restore each session mode safely', async ({ page }) => {
  await page.setViewportSize({ width: 916, height: 524 })
  const mock = await mockAgent(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  await expect(page.locator('.cc-canvas')).toBeVisible()
  await page.getByRole('button', { name: '新建对话', exact: true }).click()
  const panel = page.getByRole('complementary', { name: '创作 Agent', exact: true })
  const mode = panel.getByRole('combobox', { name: 'Agent 工作模式', exact: true })
  await expect(mode).toHaveText('策划讨论')
  const input = panel.getByLabel('向 Agent 描述创作需求', { exact: true })
  await input.fill('只策划故事，暂不生成')
  async function composerReachable() {
    await expect.poll(() => panel.evaluate(element => {
      const controls = element.querySelectorAll('.ca-composer textarea,.ca-composer-toolbar>button')
      return [...controls].every(control => {
        const box = control.getBoundingClientRect(), hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
        return box.y >= 0 && box.bottom <= innerHeight && !!hit && control.contains(hit)
      })
    })).toBe(true)
  }
  await composerReachable()
  await panel.getByRole('button', { name: 'Agent 设置', exact: true }).click()
  await expect(panel.getByLabel('Agent 服务地址', { exact: true })).toBeVisible()
  await composerReachable()
  await panel.getByRole('button', { name: '关闭 Agent 设置', exact: true }).click()
  await expect(panel.locator('.ca-settings')).toHaveCount(0)
  await panel.getByRole('button', { name: '发送给 Agent', exact: true }).click()
  await expect(panel.locator('[data-run-status="completed"]')).toBeVisible()
  expect(mock.sent[0].mode).toBe('plan')
  await page.evaluate(() => { for (const key of Object.keys(localStorage)) if (key.startsWith('canvas-agent-mode:')) localStorage.removeItem(key) })
  await page.reload()
  await page.locator('.cc-canvas-agent').click()
  await expect(panel.locator('[data-run-status="completed"]')).toBeVisible()
  await expect(mode).toHaveText('策划讨论')
  await mode.click(); await page.getByRole('option', { name: '自动创作', exact: true }).click()
  await expect(mode).toHaveText('自动创作')
  await page.reload()
  await page.locator('.cc-canvas-agent').click()
  await expect(panel.locator('[data-run-status="completed"]')).toBeVisible()
  await expect(mode).toHaveText('自动创作')
  await panel.getByRole('button', { name: '新建 Agent 会话', exact: true }).click()
  await expect(panel.locator('[data-run-status]')).toHaveCount(0)
  await expect(mode).toHaveText('策划讨论')
  await composerReachable()
  await page.screenshot({ path: '../outputs/agent-short-viewport.png', fullPage: true })
})

test('Agent retry IDs follow the full request and Enter cannot send before a session restores', async ({ page }) => {
  const mock = await mockAgent(page), attempts: any[] = []
  await page.route('**/api/agent/sessions/*/messages', async route => {
    attempts.push(route.request().postDataJSON())
    if (attempts.length < 3) await route.abort('failed')
    else await route.fallback()
  })
  await page.goto('/')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  await expect(page.locator('.cc-canvas')).toBeVisible()
  await page.getByRole('button', { name: '新建对话', exact: true }).click()
  const panel = page.getByRole('complementary', { name: '创作 Agent', exact: true }), input = panel.getByLabel('向 Agent 描述创作需求', { exact: true })
  await input.fill('一个关于灯塔的故事')
  const send = panel.getByRole('button', { name: '发送给 Agent', exact: true })
  await send.click(); await expect(panel.locator('.ca-error')).toBeVisible()
  await send.click(); await expect.poll(() => attempts.length).toBe(2)
  expect(attempts[1].request_id).toBe(attempts[0].request_id)
  await expect(input).toHaveValue('一个关于灯塔的故事')
  await panel.getByRole('combobox', { name: 'Agent 工作模式', exact: true }).click()
  await page.getByRole('option', { name: '自动创作', exact: true }).click()
  await send.click(); await expect(panel.locator('[data-run-status="completed"]')).toBeVisible()
  expect(attempts[2].request_id).not.toBe(attempts[1].request_id)
  expect(mock.sent).toHaveLength(1)
  let unlock!: () => void
  const gate = new Promise<void>(resolve => { unlock = resolve })
  await page.route('**/api/agent/sessions/session-1', async route => { await gate; await route.fallback() })
  await page.reload(); await page.locator('.cc-canvas-agent').click()
  await expect(panel.locator('.ca-loading')).toBeVisible()
  await input.fill('尚未加载完成时不能发送')
  await expect(send).toBeDisabled()
  await input.press('Enter')
  unlock()
  await expect(panel.locator('[data-run-status="completed"]')).toBeVisible()
  expect(mock.sent).toHaveLength(1)
  expect(mock.sessions).toHaveLength(1)
})

test('Agent sidebar preserves IME caret, references selected nodes, saves protocols and restores tool history', async ({ page }) => {
  const mock = await mockAgent(page), errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  await expect(page.locator('.cc-canvas')).toBeVisible()
  await page.keyboard.press('i')
  const nodeId = await page.locator('.react-flow__node').first().getAttribute('data-id')
  await page.getByRole('button', { name: '新建对话', exact: true }).click()
  const panel = page.getByRole('complementary', { name: '创作 Agent', exact: true })
  await expect(panel).toBeVisible()
  await expect(panel.locator('.ca-context-chip')).toHaveCount(1)
  const input = panel.getByLabel('向 Agent 描述创作需求', { exact: true })
  await expect.poll(async () => {
    const area = await input.boundingBox(), box = await panel.boundingBox()
    return !!area && !!box && area.width > box.width - 70
  }).toBe(true)
  const toolbar = panel.locator('.ca-composer-toolbar')
  const modeSelect = panel.getByRole('combobox', { name: 'Agent 工作模式', exact: true })
  expect(await modeSelect.evaluate(element => ({ width: element.getBoundingClientRect().width, fits: element.scrollWidth <= element.clientWidth + 1, display: getComputedStyle(element).display })))
    .toMatchObject({ fits: true, display: 'flex' })
  expect((await modeSelect.boundingBox())!.width).toBeGreaterThan(80)
  expect(await toolbar.evaluate(element => {
    const controls = [...element.querySelectorAll(':scope>button,:scope>.cc-glass-select')].map(control => control.getBoundingClientRect())
    return controls.every((control, index) => control.width >= 27 && (index === 0 || control.left >= controls[index - 1].right - 1))
  })).toBe(true)
  await input.fill('开头结尾')
  await input.press('Home'); await input.press('ArrowRight'); await input.press('ArrowRight')
  const cdp = await page.context().newCDPSession(page)
  await cdp.send('Input.imeSetComposition', { text: 'zhong', selectionStart: 5, selectionEnd: 5 })
  await page.waitForResponse(response => response.url().endsWith('/api/state'))
  await expect(input).toHaveValue('开头zhong结尾')
  await cdp.send('Input.imeSetComposition', { text: '中文', selectionStart: 2, selectionEnd: 2 })
  await cdp.send('Input.insertText', { text: '中文' })
  await input.pressSequentially('输入')
  await expect(input).toHaveValue('开头中文输入结尾')
  await panel.getByRole('button', { name: '引用画布节点', exact: true }).click()
  await panel.locator('.ca-mentions>button').first().click()
  await expect(input).toHaveValue(`开头中文输入@[node:${nodeId}] 结尾`)
  await panel.getByRole('button', { name: 'Agent 设置', exact: true }).click()
  await panel.getByRole('combobox', { name: 'Agent 接口协议', exact: true }).click()
  await page.getByRole('option', { name: 'Chat Completions', exact: true }).click()
  await panel.getByRole('button', { name: '保存并测试', exact: true }).click()
  await expect(panel.getByRole('status')).toHaveText('模型工具连接正常')
  expect(mock.savedSettings.at(-1).protocol).toBe('chat_completions')
  expect(mock.savedSettings.at(-1).strict_tools).toBe(false)
  expect(mock.savedSettings.at(-1).api_key).toBeUndefined()
  await panel.getByRole('button', { name: '关闭 Agent 设置', exact: true }).click()
  await input.fill((await input.inputValue()) + '\n' + '分镜段落：海边机器人守护一只小鸟。\n'.repeat(45))
  await panel.getByRole('button', { name: '发送给 Agent', exact: true }).click()
  await expect(panel.locator('[data-run-status="completed"]')).toBeVisible()
  expect(mock.sent).toHaveLength(1)
  expect(mock.sent[0].selected_nodes).toEqual([nodeId])
  expect(mock.sent[0].message).toContain(`@[node:${nodeId}]`)
  expect(mock.sent[0].request_id).toBeTruthy()
  await expect(panel.getByText('分镜和连线已准备好。', { exact: true })).toBeVisible()
  await expect(panel.locator('.ca-assistant-text strong')).toHaveText('角色固定')
  await expect(panel.locator('.ca-assistant-text li')).toHaveCount(4)
  await expect(panel.locator('.ca-assistant-text code')).toHaveText('黄铜机器人')
  await expect(panel.getByRole('link', { name: '本地素材', exact: true })).toHaveAttribute('href', '/api/assets/example/file')
  await expect(panel.getByRole('link', { name: '参考', exact: true })).toHaveAttribute('rel', 'noopener noreferrer')
  await expect(panel.getByRole('link', { name: '危险', exact: true })).toHaveCount(0)
  await expect(panel.locator('.ca-assistant-text img')).toHaveCount(0)
  await expect(panel.locator('.ca-assistant-text')).toContainText('<img src=x onerror=alert(1)>')
  await expect(panel.getByRole('button', { name: '展开全部', exact: true })).toHaveAttribute('aria-expanded', 'false')
  await expect.poll(() => panel.locator('.ca-conversation').evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(20)
  await panel.locator('.ca-tool-heading').click()
  await expect(panel.locator('.ca-tool-body')).toContainText('node_count')
  await expect(panel.locator('.ca-tool-body')).not.toContainText('never-render-this-secret')
  await expect(panel.locator('.ca-cache').first()).toHaveText('缓存 80%')
  await panel.getByRole('button', { name: '收起 Agent', exact: true }).click()
  await expect(panel).toHaveCount(0)
  await page.locator('.cc-agent-conversations').getByRole('button', { name: '灯塔的故事', exact: true }).click()
  await expect(panel.getByText('分镜和连线已准备好。', { exact: true })).toBeVisible()
  await expect.poll(() => panel.locator('.ca-conversation').evaluate(element => element.scrollHeight - element.scrollTop - element.clientHeight)).toBeLessThan(20)
  await expect(panel.locator('.ca-tool-heading')).toHaveCount(1)
  expect(errors).toEqual([])
})

test('Agent additions fit the workflow once and the narrow toolbar stays on one line', async ({ page }) => {
  await page.setViewportSize({ width: 916, height: 800 })
  await mockAgent(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  await expect(page.locator('.cc-canvas')).toBeVisible()
  await page.getByRole('button', { name: '新建对话', exact: true }).click()
  await expect(page.getByRole('complementary', { name: '创作 Agent', exact: true })).toBeVisible()
  const id = new URL(page.url()).hash.replace('#project=', ''), { token } = await (await page.request.get('/api/state')).json()
  const remote = await (await page.request.get('/api/projects/' + id)).json()
  remote.canvases[0].nodes = [0, 1, 2].map(index => ({ id: 'shot-' + index, type: 'creative', position: { x: 700 + index * 430, y: 700 }, data: { kind: 'image', label: 'Shot ' + index, prompt: 'A cinematic frame', model: 'minimax-image' } }))
  expect((await page.request.put('/api/projects/' + id, { headers: { 'X-Workbench-Token': token }, data: remote })).ok()).toBeTruthy()
  await expect(page.locator('.react-flow__node')).toHaveCount(3)
  await expect.poll(async () => {
    const canvas = await page.locator('.cc-canvas').boundingBox()
    const nodes = await page.locator('.react-flow__node').evaluateAll(elements => elements.map(element => { const box = element.getBoundingClientRect(); return { x: box.x, y: box.y, right: box.right, bottom: box.bottom } }))
    return !!canvas && nodes.every(node => node.x >= canvas.x && node.right <= canvas.x + canvas.width && node.y >= canvas.y && node.bottom <= canvas.y + canvas.height)
  }).toBe(true)
  const toolbar = page.locator('.cc-canvas-toolbar')
  await expect(page.getByRole('button', { name: '原始画布', exact: true })).toBeHidden()
  expect(await toolbar.evaluate(element => {
    const parent = element.getBoundingClientRect()
    return [...element.querySelectorAll(':scope>button,:scope>div')].filter(child => getComputedStyle(child).display !== 'none').every(child => { const box = child.getBoundingClientRect(); return box.height < 50 && box.x >= parent.x - 1 && box.right <= parent.right + 1 && getComputedStyle(child).whiteSpace === 'nowrap' })
  })).toBe(true)
  const transform = await page.locator('.react-flow__viewport').evaluate(element => new Promise<string>((resolve, reject) => {
    let previous = '', stableFrames = 0, sampledFrames = 0
    function sample() {
      const current = element.getAttribute('style') ?? ''
      stableFrames = current === previous ? stableFrames + 1 : 0
      previous = current
      if (stableFrames >= 12) resolve(current)
      else if (++sampledFrames > 240) reject(new Error('自动缩放后的画布视角仍未稳定'))
      else requestAnimationFrame(sample)
    }
    requestAnimationFrame(sample)
  }))
  const updated = await (await page.request.get('/api/projects/' + id)).json()
  updated.canvases[0].nodes[0].data.prompt = 'Only the prompt changes'
  expect((await page.request.put('/api/projects/' + id, { headers: { 'X-Workbench-Token': token }, data: updated })).ok()).toBeTruthy()
  await expect(page.locator('.react-flow__node[data-id="shot-0"]')).toContainText('Only the prompt changes')
  expect(await page.locator('.react-flow__viewport').getAttribute('style')).toBe(transform)
})

test('autosave rebases on a concurrent Agent revision instead of overwriting its nodes', async ({ page }) => {
  await mockAgent(page)
  await page.goto('/')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  await expect(page.locator('.cc-canvas')).toBeVisible()
  await page.keyboard.press('i')
  const prompt = page.getByLabel('描述这张图片', { exact: true })
  await prompt.fill('Original')
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  const id = new URL(page.url()).hash.replace('#project=', '')
  const { token } = await (await page.request.get('/api/state')).json()
  let conflicted = false
  await page.route(`**/api/projects/${id}`, async route => {
    if (route.request().method() === 'PUT' && !conflicted) {
      conflicted = true
      const remote = await (await page.request.get('/api/projects/' + id)).json()
      remote.canvases[0].nodes.push({ id: 'agent-added', type: 'creative', position: { x: 600, y: 100 }, data: { kind: 'text', label: 'Agent 分镜', prompt: 'A quiet ending' } })
      expect((await page.request.put('/api/projects/' + id, { headers: { 'X-Workbench-Token': token }, data: remote })).ok()).toBeTruthy()
      await route.fulfill({ status: 409, json: { detail: '项目已被 Agent 更新' } })
      return
    }
    await route.continue()
  })
  await prompt.fill('Manual change during Agent run')
  await expect.poll(() => conflicted).toBe(true)
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  const saved = await (await page.request.get('/api/projects/' + id)).json()
  expect(saved.canvases[0].nodes).toHaveLength(2)
  expect(saved.canvases[0].nodes.find((node: any) => node.id !== 'agent-added').data.prompt).toBe('Manual change during Agent run')
  await page.reload()
  await expect(page.locator('.react-flow__node[data-id="agent-added"]')).toBeVisible()
  await page.locator('.react-flow__node').filter({ has: page.getByTestId('card-image') }).click()
  await expect(prompt).toHaveValue('Manual change during Agent run')
})
