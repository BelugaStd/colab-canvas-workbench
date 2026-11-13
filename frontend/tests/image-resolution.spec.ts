import { test, expect } from '@playwright/test'

test('MiniMax native 4K persists and submits a 4096-square workflow', async ({ page }) => {
  await page.goto('/')
  const { token } = await (await page.request.get('/api/state')).json()
  const headers = { 'X-Workbench-Token': token }
  const project = await (await page.request.post('/api/projects', { headers, data: { name: 'Native 4K acceptance' } })).json()
  project.canvases[0].viewport = { x: 50, y: 20, zoom: .9 }
  project.canvases[0].nodes = [{ id: 'image', type: 'creative', position: { x: 100, y: 0 }, data: {
    kind: 'image', label: 'Native 4K', prompt: 'A blue cup on a table', model: 'minimax-image',
    image_mode: 'text', resolution: '2K', aspect: '1:1', quality: '标准',
  } }]
  expect((await page.request.put('/api/projects/' + project.id, { headers, data: project })).ok()).toBeTruthy()
  await page.goto('/?native4k=1#project=' + project.id)
  await page.getByTestId('card-image').click()
  await page.getByRole('button', { name: '节点设置', exact: true }).click()
  await page.getByRole('button', { name: '4K', exact: true }).click()
  await expect(page.getByText(/官方全量 BF16.*原生 4K/)).toBeVisible()
  await page.getByRole('button', { name: '关闭设置', exact: true }).click()
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  await page.reload()
  await page.getByTestId('card-image').click()
  await page.getByRole('button', { name: '节点设置', exact: true }).click()
  await expect(page.getByRole('button', { name: '4K', exact: true })).toHaveClass('active')
  await page.getByRole('button', { name: '关闭设置', exact: true }).click()
  const generated = page.waitForResponse(r => r.url().endsWith('/nodes/image/generate') && r.request().method() === 'POST')
  await page.getByRole('button', { name: '生成', exact: true }).click()
  const job = await (await generated).json()
  const native = Object.values(job.graph).find((n: any) => n.class_type === 'MiniMaxH3ImageConditioning') as any
  expect([native.inputs.width, native.inputs.height]).toEqual([4096, 4096])
  expect(job.creative.node_settings.image.resolution).toBe('4K')
  expect(job.creative.node_dimensions.image).toEqual([4096, 4096])
  expect(job.creative.models).toEqual(['minimax-image-full'])
  expect(Object.values(job.graph).some((n: any) => n.class_type === 'WorkbenchMiniMaxImageFullLoader')).toBeTruthy()
  await expect(page.getByRole('button', { name: '版本 1，当前输出', exact: true })).toBeVisible({ timeout: 20000 })
})
