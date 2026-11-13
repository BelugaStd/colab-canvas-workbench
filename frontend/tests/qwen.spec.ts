import { test, expect } from '@playwright/test'

test('Qwen Edit requires images, saves its settings and records native reference inputs',async({page})=>{
  await page.goto('/')
  const {token}=await (await page.request.get('/api/state')).json()
  const headers={'X-Workbench-Token':token}
  const project=await (await page.request.post('/api/projects',{headers,data:{name:'Qwen edit acceptance'}})).json()
  project.canvases[0].viewport={x:50,y:20,zoom:.9}
  project.canvases[0].nodes=[{id:'edit',type:'creative',position:{x:100,y:0},data:{kind:'image',label:'Qwen test',prompt:'将@图1的杯子改为蓝色',model:'minimax-image',resolution:'2K',aspect:'source',quality:'标准'}}]
  expect((await page.request.put('/api/projects/'+project.id,{headers,data:project})).ok()).toBeTruthy()
  await page.goto('/?qwen=1#project='+project.id)
  await page.getByTestId('card-image').click()
  await page.getByRole('button',{name:'节点设置',exact:true}).click()
  await page.getByRole('button',{name:'Qwen Edit',exact:true}).click()
  await expect(page.getByRole('button',{name:'1MP',exact:true})).toHaveClass('active')
  await expect(page.getByRole('button',{name:'生成',exact:true})).toBeDisabled()
  await page.getByRole('button',{name:'关闭设置',exact:true}).click()
  await expect(page.locator('.cc-sidebar-save')).toHaveText('已保存')
  await page.reload();await page.getByTestId('card-image').click()
  await expect(page.locator('.cc-model-label')).toHaveText('Qwen Edit')
  const current=await (await page.request.get('/api/projects/'+project.id)).json()
  current.canvases[0].nodes.push({id:'source',type:'creative',position:{x:650,y:0},data:{kind:'image',label:'source',prompt:'A cup',model:'minimax-image',resolution:'1K'}})
  current.canvases[0].edges=[{id:'ref',source:'source',target:'edit',data:{alias:'图1',role:'base'}}]
  expect((await page.request.put('/api/projects/'+project.id,{headers,data:current})).ok()).toBeTruthy()
  await page.reload();await page.locator('.react-flow__node[data-id="edit"]').getByTestId('card-image').click()
  const generated=page.waitForResponse(r=>r.url().endsWith('/nodes/edit/generate')&&r.request().method()==='POST')
  await page.getByRole('button',{name:'生成',exact:true}).click()
  const job=await (await generated).json()
  expect(job.creative.models).toContain('qwen-edit')
  expect(Object.values(job.graph).some((n:any)=>n.class_type==='TextEncodeQwenImageEditPlus'&&'image1' in n.inputs)).toBeTruthy()
  await expect(page.locator('.react-flow__node[data-id="edit"]').getByRole('button',{name:'版本 1，当前输出',exact:true})).toBeVisible({timeout:20000})
})
