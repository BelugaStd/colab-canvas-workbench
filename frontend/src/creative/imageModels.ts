export const imageModels = [
  {value:'minimax-image', label:'MiniMax Image', maxReferences:10, resolutions:['1K','2K']},
  {value:'moss-image', label:'MOSS Image', maxReferences:4, resolutions:['1MP','1.5MP','2MP']},
  {value:'qwen-edit', label:'Qwen Edit', maxReferences:3, resolutions:['1MP']},
]

export const imageModel = (value?:string) => imageModels.find(model=>model.value===value) ?? imageModels[0]
