import { test, expect } from '@playwright/test'
import { readFileSync } from 'node:fs'

test('hash-only project navigation and browser history flush edits before switching', async ({ page }) => {
  await page.goto('/')
  const { token } = await (await page.request.get('/api/state')).json()
  const headers = { 'X-Workbench-Token': token }, projects: any[] = []
  for (const name of ['Hash project A', 'Hash project B']) {
    const project = await (await page.request.post('/api/projects', { headers, data: { name } })).json()
    project.canvases[0].nodes = [{ id: 'image', type: 'creative', position: { x: 100, y: 100 }, data: { kind: 'image', label: name, prompt: name, model: 'minimax-image' } }]
    projects.push(await (await page.request.put('/api/projects/' + project.id, { headers, data: project })).json())
  }
  const [first, second] = projects
  await page.goto('/?hash-navigation=1#project=' + first.id)
  await expect(page.locator('.cc-project-name')).toHaveText(first.name)
  await page.evaluate(() => { (window as any).__hashDocument = 'same document' })
  await page.getByTestId('card-image').click()
  const input = page.getByLabel('描述这张图片', { exact: true })
  let unlock!: () => void
  const gate = new Promise<void>(resolve => { unlock = resolve })
  let intercepted = false
  await page.route(`**/api/projects/${first.id}`, async route => {
    if (route.request().method() === 'PUT' && !intercepted) { intercepted = true; await gate }
    await route.continue()
  })
  await input.fill('First save before hash change')
  await page.evaluate(id => { location.hash = 'project=' + id }, second.id)
  await expect.poll(() => intercepted).toBe(true)
  await input.fill('Latest edit while hash navigation waits')
  unlock()
  await expect(page.locator('.cc-project-name')).toHaveText(second.name)
  await page.getByTestId('card-image').click()
  await input.fill('Edited B before browser back')
  await page.goBack()
  await expect(page.locator('.cc-project-name')).toHaveText(first.name)
  await page.getByTestId('card-image').click()
  await expect(input).toHaveValue('Latest edit while hash navigation waits')
  await page.goForward()
  await expect(page.locator('.cc-project-name')).toHaveText(second.name)
  await page.getByTestId('card-image').click()
  await expect(input).toHaveValue('Edited B before browser back')
  await page.evaluate(() => { location.hash = '' })
  await expect(page.locator('.cc-home-page')).toBeVisible()
  await page.goBack()
  await expect(page.locator('.cc-project-name')).toHaveText(second.name)
  await page.getByTestId('card-image').click()
  await expect(input).toHaveValue('Edited B before browser back')
  expect(await page.evaluate(() => (window as any).__hashDocument)).toBe('same document')
})

test('leaving a project saves edits made while its first save is still pending', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '新建项目', exact: true }).first().click()
  await expect(page.locator('.cc-canvas')).toBeVisible()
  const projectId = new URL(page.url()).hash.replace('#project=', '')
  await page.keyboard.press('i')
  const input = page.getByLabel('描述这张图片', { exact: true })
  await input.fill('Initial prompt')
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  let unlock!: () => void
  const gate = new Promise<void>(resolve => { unlock = resolve })
  let intercepted = false
  await page.route(`**/api/projects/${projectId}`, async route => {
    if (route.request().method() === 'PUT' && !intercepted) {
      intercepted = true
      await gate
    }
    await route.continue()
  })
  await input.fill('First save snapshot')
  await page.getByRole('button', { name: '返回工作区', exact: true }).click()
  await expect.poll(() => intercepted).toBe(true)
  await input.fill('Latest edit while saving')
  unlock()
  await expect(page.locator('.cc-home-page')).toBeVisible()
  await page.goto(`/?reopen=1#project=${projectId}`)
  await page.getByTestId('card-image').click()
  await expect(input).toHaveValue('Latest edit while saving')
})

test('Chinese composition survives polling and commits at the middle caret',async({page})=>{
  await page.goto('/')
  await page.getByRole('button',{name:'新建项目',exact:true}).first().click()
  await expect(page.locator('.cc-canvas')).toBeVisible()
  await page.keyboard.press('i')
  const input=page.getByLabel('描述这张图片',{exact:true})
  await input.fill('开头结尾')
  await input.press('Home');await input.press('ArrowRight');await input.press('ArrowRight')
  const cdp=await page.context().newCDPSession(page)
  await cdp.send('Input.imeSetComposition',{text:'zhong',selectionStart:5,selectionEnd:5})
  await page.waitForResponse(r=>r.url().endsWith('/api/state'))
  await expect(input).toHaveValue('开头zhong结尾')
  await cdp.send('Input.imeSetComposition',{text:'中文',selectionStart:2,selectionEnd:2})
  await cdp.send('Input.insertText',{text:'中文'})
  await expect(input).toHaveValue('开头中文结尾')
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  await input.pressSequentially('输入')
  await expect(input).toHaveValue('开头中文输入结尾')
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  await page.reload();await page.getByTestId('card-image').click()
  await expect(input).toHaveValue('开头中文输入结尾')
})

test('reference roles, caret mentions and disconnected aliases stay explicit',async({page})=>{
  await page.goto('/')
  const {token}=await (await page.request.get('/api/state')).json()
  const headers={'X-Workbench-Token':token}
  const project=await (await page.request.post('/api/projects',{headers,data:{name:'Reference regression'}})).json()
  const canvas=project.canvases[0]
  const node=(id:string,kind:string,x:number,y:number,data={})=>({id,type:'creative',position:{x,y},data:{kind,label:kind==='image'?'Same name':id,prompt:'test',...data}})
  canvas.viewport={x:10,y:10,zoom:.8}
  canvas.nodes=[node('a','image',0,0),node('b','image',0,450),node('v','video',470,100,{prompt:'Start  continue',mode:'首尾帧',duration:5,fps:24,resolution:'512p',aspect:'source'})]
  canvas.edges=[{id:'e1',source:'a',target:'v',data:{alias:'图1',role:'first'}},{id:'e2',source:'b',target:'v',data:{alias:'图2',role:'last'}}]
  expect((await page.request.put('/api/projects/'+project.id,{headers,data:project})).ok()).toBeTruthy()
  await page.goto('/?reference-test=1#project='+project.id)
  await page.locator('.react-flow__node[data-id="v"]').getByTestId('card-video').click()
  const input=page.getByLabel('描述这段视频',{exact:true})
  await input.press('Home');for(let i=0;i<6;i++)await input.press('ArrowRight')
  await input.pressSequentially('@')
  await page.locator('.cc-mentions').getByRole('button',{name:'@图2 · Same name 等待生成',exact:true}).click()
  await expect(input).toHaveValue('Start @图2  continue')
  await page.getByRole('combobox',{name:'图2 的参考用途',exact:true}).click()
  await page.getByRole('option',{name:'首帧',exact:true}).click()
  await expect(page.getByRole('combobox',{name:'图1 的参考用途',exact:true})).toHaveText('尾帧')
  await page.getByRole('button',{name:'移除引用 图2',exact:true}).click()
  await expect(page.getByRole('button',{name:'生成',exact:true})).toBeDisabled()
  await expect(page.getByText('引用 @图2 已断开，请重新插入或删除这段引用',{exact:true})).toBeVisible()
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  await page.reload();await page.locator('.react-flow__node[data-id="v"]').getByTestId('card-video').click()
  await expect(input).toHaveValue('Start @图2  continue')
  await expect(page.getByRole('button',{name:'生成',exact:true})).toBeDisabled()
})

test('regeneration keeps versions and downstream uses the output selected by double click',async({page})=>{
  await page.goto('/')
  const {token}=await (await page.request.get('/api/state')).json()
  const headers={'X-Workbench-Token':token}
  const project=await (await page.request.post('/api/projects',{headers,data:{name:'Version references'}})).json()
  const canvas=project.canvases[0]
  canvas.viewport={x:20,y:20,zoom:.8}
  canvas.nodes=[['source',0],['edit',500]].map(([id,x])=>({id,type:'creative',position:{x,y:100},data:{kind:'image',label:id,prompt:id==='source'?'A red cup':'Change @图1 to blue',image_mode:id==='source'?'text':'edit',model:'minimax-image',resolution:'2K'}}))
  canvas.edges=[{id:'reference',source:'source',target:'edit',data:{alias:'图1',role:'base'}}]
  expect((await page.request.put('/api/projects/'+project.id,{headers,data:project})).ok()).toBeTruthy()
  await page.goto('/?versions=1#project='+project.id)
  const source=page.locator('.react-flow__node[data-id="source"]')
  await source.getByTestId('card-image').click()
  await source.getByRole('button',{name:'生成',exact:true}).click()
  await expect(source.getByRole('button',{name:'版本 1，当前输出',exact:true})).toBeVisible({timeout:20000})
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  const first=(await (await page.request.get('/api/projects/'+project.id)).json()).canvases[0].nodes.find((n:{id:string})=>n.id==='source').data.asset_id
  await source.getByRole('button',{name:'查看节点详情',exact:true}).click()
  const details=page.getByRole('dialog',{name:'节点详情',exact:true})
  await details.getByLabel('详情提示词',{exact:true}).fill('A green cup')
  await details.getByRole('button',{name:'重新生成',exact:true}).click()
  await expect(details.getByRole('button',{name:'版本 2，当前输出',exact:true})).toBeVisible({timeout:20000})
  await expect(details.locator('.cc-version-description p')).toHaveText('A green cup')
  await details.getByRole('button',{name:'版本 1',exact:true}).click()
  await expect(details.locator('.cc-version-description p')).toHaveText('A red cup')
  await details.getByRole('button',{name:'关闭节点详情',exact:true}).click()
  await source.getByRole('button',{name:'版本 1',exact:true}).dblclick()
  await expect(source.getByRole('button',{name:'版本 1，当前输出',exact:true})).toBeVisible()
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  const edit=page.locator('.react-flow__node[data-id="edit"]')
  await edit.getByTestId('card-image').click()
  const response=page.waitForResponse(r=>r.url().endsWith('/nodes/edit/generate')&&r.request().method()==='POST')
  await edit.getByRole('button',{name:'生成',exact:true}).click()
  const job=await (await response).json()
  expect(job.creative.node_settings.edit.reference_inputs[0].asset_id).toBe(first)
  expect(Object.values(job.graph).some((n:any)=>n.class_type==='LoadImage'&&n.inputs.image===`creative-${first}.png`)).toBeTruthy()
  await expect(edit.getByRole('button',{name:'版本 1，当前输出',exact:true})).toBeVisible({timeout:20000})
  await edit.getByRole('button',{name:'查看节点详情',exact:true}).click()
  await expect(details.getByText('生成时输入',{exact:true})).toBeVisible()
  await expect(details.locator('.cc-reference-chip').filter({hasText:'@图1 · source · 主图'})).toBeVisible()
})

test('selecting a video version preserves the swapped frame roles',async({page})=>{
  await page.goto('/')
  const {token}=await (await page.request.get('/api/state')).json()
  const headers={'X-Workbench-Token':token}
  const media=readFileSync('../tests/fixtures/synthetic.mp4')
  const assets=[]
  for(let i=0;i<2;i++)assets.push(await (await page.request.post('/api/assets',{headers:{...headers,'Content-Type':'application/octet-stream'},data:media})).json())
  const project=await (await page.request.post('/api/projects',{headers,data:{name:'Version frame roles'}})).json()
  const canvas=project.canvases[0]
  canvas.viewport={x:20,y:20,zoom:.65}
  canvas.nodes=['a','b','video'].map((id,i)=>({id,type:'creative',position:{x:i===2?500:0,y:i===1?400:0},data:{kind:i===2?'video':'image',label:id,prompt:'test'}}))
  Object.assign(canvas.nodes[2].data,{mode:'首尾帧',duration:5,fps:24,asset_id:assets[0].id,media_type:'video/mp4',active_version_id:assets[0].id,
    versions:assets.map(a=>({id:a.id,asset_id:a.id,media_type:'video/mp4',created_at:a.created_at}))})
  canvas.edges=[{id:'first',source:'a',target:'video',data:{alias:'图1',role:'first'}},{id:'last',source:'b',target:'video',data:{alias:'图2',role:'last'}}]
  expect((await page.request.put('/api/projects/'+project.id,{headers,data:project})).ok()).toBeTruthy()
  await page.goto('/?roles=1#project='+project.id)
  const video=page.locator('.react-flow__node[data-id="video"]')
  await video.getByTestId('card-video').click()
  await page.getByRole('combobox',{name:'图2 的参考用途',exact:true}).click()
  await page.getByRole('option',{name:'首帧',exact:true}).click()
  await expect(page.getByRole('combobox',{name:'图1 的参考用途',exact:true})).toHaveText('尾帧')
  await video.getByRole('button',{name:'版本 2',exact:true}).dblclick()
  await expect(video.getByRole('button',{name:'版本 2，当前输出',exact:true})).toBeVisible()
  await expect(page.getByRole('combobox',{name:'图1 的参考用途',exact:true})).toHaveText('尾帧')
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  await page.reload();await video.getByTestId('card-video').click()
  await expect(page.getByRole('combobox',{name:'图2 的参考用途',exact:true})).toHaveText('首帧')
})

test('clicking the selected image model keeps its chosen resolution',async({page})=>{
  await page.goto('/')
  await page.getByRole('button',{name:'新建项目',exact:true}).first().click()
  await expect(page.locator('.cc-canvas')).toBeVisible()
  await page.keyboard.press('i')
  await page.getByRole('button',{name:'节点设置',exact:true}).click()
  await page.getByRole('button',{name:'1K',exact:true}).click()
  await page.getByRole('button',{name:'MiniMax Image',exact:true}).click()
  await expect(page.getByRole('button',{name:'1K',exact:true})).toHaveClass('active')
  await page.getByRole('button',{name:'MOSS Image',exact:true}).click()
  await page.getByRole('button',{name:'1.5MP',exact:true}).click()
  await page.getByRole('button',{name:'MOSS Image',exact:true}).click()
  await expect(page.getByRole('button',{name:'1.5MP',exact:true})).toHaveClass('active')
  await page.getByRole('button',{name:'关闭设置',exact:true}).click()
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  await page.reload();await page.getByTestId('card-image').click()
  await page.getByRole('button',{name:'节点设置',exact:true}).click()
  await expect(page.getByRole('button',{name:'1.5MP',exact:true})).toHaveClass('active')
})

test('IME cancellation does not dismiss the project name dialog',async({page})=>{
  await page.goto('/')
  await page.getByRole('button',{name:'新建文件夹',exact:true}).click()
  const dialog=page.getByRole('dialog',{name:'新建文件夹',exact:true})
  const input=dialog.getByRole('textbox',{name:'名称',exact:true})
  await input.fill('中文测试')
  await input.dispatchEvent('keydown',{key:'Escape',code:'Escape',isComposing:true,bubbles:true})
  await page.waitForResponse(r=>r.url().endsWith('/api/state'))
  await expect(dialog).toBeVisible()
  await expect(input).toHaveValue('中文测试')
  await input.press('Escape')
  await expect(dialog).toHaveCount(0)
})
