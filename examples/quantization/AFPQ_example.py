import os
from auto_gptq import AutoGPTQForCausalLM, BaseQuantizeConfig
import numpy as np
import torch
import torch.nn as nn
import time
from logging import getLogger
logger = getLogger(__name__)

CACHE_DIR = None

# os.makedirs(quantized_model_dir, exist_ok=True)
def get_wikitext2(nsamples, seed, seqlen, model):
    from datasets import load_dataset
    traindata = load_dataset('wikitext', 'wikitext-2-raw-v1', split='train')
    testdata = load_dataset('wikitext', 'wikitext-2-raw-v1', split='test')

    from transformers import AutoTokenizer, LlamaTokenizer
    # if "llama" in model:
    #     tokenizer = LlamaTokenizer.from_pretrained(model, use_fast=False)
    # else:
    try:
        tokenizer = AutoTokenizer.from_pretrained(model, use_fast=False)
    except:
        tokenizer = AutoTokenizer.from_pretrained(model, use_fast=True)
    trainenc = tokenizer("\n\n".join(traindata['text']), return_tensors='pt')
    testenc = tokenizer("\n\n".join(testdata['text']), return_tensors='pt')

    import random
    random.seed(seed)
    np.random.seed(0)
    torch.random.manual_seed(0)
    
    traindataset = []
    for _ in range(nsamples):
        i = random.randint(0, trainenc.input_ids.shape[1] - seqlen - 1)
        j = i + seqlen
        inp = trainenc.input_ids[:, i:j]
        attention_mask = torch.ones_like(inp)
        traindataset.append({'input_ids':inp,'attention_mask': attention_mask})
    return traindataset, testenc

@torch.no_grad()
def llama_eval(model, testenc, dev, seqlen = 2048):
    from tqdm import tqdm
    print('Evaluating ...')

    testenc = testenc.input_ids
    nsamples = testenc.numel() // seqlen

    use_cache = model.config.use_cache
    model.config.use_cache = False
    layers = model.model.layers

    model.model.embed_tokens = model.model.embed_tokens.to(dev)
    layers[0] = layers[0].to(dev)

    dtype = next(iter(model.parameters())).dtype
    inps = torch.zeros((nsamples, seqlen, model.config.hidden_size), dtype=dtype, device=dev)
    cache = {'i': 0, 'attention_mask': None}

    class Catcher(nn.Module):

        def __init__(self, module):
            super().__init__()
            self.module = module

        def forward(self, inp, **kwargs):
            inps[cache['i']] = inp
            cache['i'] += 1
            cache['attention_mask'] = kwargs['attention_mask']
            cache['position_ids'] = kwargs['position_ids']
            raise ValueError

    layers[0] = Catcher(layers[0])
    for i in range(nsamples):
        batch = testenc[:, (i * seqlen):((i + 1) * seqlen)].to(dev)
        try:
            model(batch)
        except ValueError:
            pass
    layers[0] = layers[0].module

    layers[0] = layers[0].cpu()
    model.model.embed_tokens = model.model.embed_tokens.cpu()
    torch.cuda.empty_cache()

    outs = torch.zeros_like(inps)
    attention_mask = cache['attention_mask']
    position_ids = cache['position_ids']

    for i in tqdm(range(len(layers))):
        # print('layer', i)
        layer = layers[i].to(dev)

        for j in range(nsamples):
            outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask, position_ids=position_ids)[0]
        layers[i] = layer.cpu()
        del layer
        torch.cuda.empty_cache()
        inps, outs = outs, inps

    if model.model.norm is not None:
        model.model.norm = model.model.norm.to(dev)
    model.lm_head = model.lm_head.to(dev)

    testenc = testenc.to(dev)
    nlls = []
    for i in tqdm(range(nsamples)):
        hidden_states = inps[i].unsqueeze(0)
        if model.model.norm is not None:
            hidden_states = model.model.norm(hidden_states)
        lm_logits = model.lm_head(hidden_states)
        shift_logits = lm_logits[:, :-1, :].contiguous()
        shift_labels = testenc[:, (i * seqlen):((i + 1) * seqlen)][:, 1:]
        loss_fct = nn.CrossEntropyLoss()
        loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
        neg_log_likelihood = loss.float() * seqlen
        nlls.append(neg_log_likelihood)
    ppl = torch.exp(torch.stack(nlls).sum() / (nsamples * seqlen))
    print('ppl: ')
    print(ppl.item())
    print()

    model.config.use_cache = use_cache

@torch.no_grad()
def opt_eval(model, testenc, dev, seqlen = 2048):
    print('Evaluating ...')

    testenc = testenc.input_ids
    nsamples = testenc.numel() // seqlen

    use_cache = model.config.use_cache
    model.config.use_cache = False
    layers = model.model.decoder.layers

    model.model.decoder.embed_tokens = model.model.decoder.embed_tokens.to(dev)
    model.model.decoder.embed_positions = model.model.decoder.embed_positions.to(dev)
    if hasattr(model.model.decoder, 'project_out') and model.model.decoder.project_out:
        model.model.decoder.project_out = model.model.decoder.project_out.to(dev)
    if hasattr(model.model.decoder, 'project_in') and model.model.decoder.project_in:
        model.model.decoder.project_in = model.model.decoder.project_in.to(dev)
    layers[0] = layers[0].to(dev)

    dtype = next(iter(model.parameters())).dtype
    inps = torch.zeros((nsamples, seqlen, model.config.hidden_size), dtype=dtype, device=dev)
    cache = {'i': 0, 'attention_mask': None}

    class Catcher(nn.Module):

        def __init__(self, module):
            super().__init__()
            self.module = module

        def forward(self, inp, **kwargs):
            inps[cache['i']] = inp
            cache['i'] += 1
            cache['attention_mask'] = kwargs['attention_mask']
            raise ValueError

    layers[0] = Catcher(layers[0])
    for i in range(nsamples):
        batch = testenc[:, (i * seqlen):((i + 1) * seqlen)].to(dev)
        try:
            model(batch)
        except ValueError:
            pass
    layers[0] = layers[0].module

    layers[0] = layers[0].cpu()
    model.model.decoder.embed_tokens = model.model.decoder.embed_tokens.cpu()
    model.model.decoder.embed_positions = model.model.decoder.embed_positions.cpu()
    if hasattr(model.model.decoder, 'project_out') and model.model.decoder.project_out:
        model.model.decoder.project_out = model.model.decoder.project_out.cpu()
    if hasattr(model.model.decoder, 'project_in') and model.model.decoder.project_in:
        model.model.decoder.project_in = model.model.decoder.project_in.cpu()
    torch.cuda.empty_cache()

    outs = torch.zeros_like(inps)
    attention_mask = cache['attention_mask']

    for i in range(len(layers)):
        print(i)
        layer = layers[i].to(dev)

        for j in range(nsamples):
            outs[j] = layer(inps[j].unsqueeze(0), attention_mask=attention_mask)[0]
        layers[i] = layer.cpu()
        del layer
        torch.cuda.empty_cache()
        inps, outs = outs, inps

    if model.model.decoder.final_layer_norm is not None:
        model.model.decoder.final_layer_norm = model.model.decoder.final_layer_norm.to(dev)
    if model.model.decoder.project_out is not None:
        model.model.decoder.project_out = model.model.decoder.project_out.to(dev)
    model.lm_head = model.lm_head.to(dev)

    testenc = testenc.to(dev)
    nlls = []
    for i in range(nsamples):
        hidden_states = inps[i].unsqueeze(0)
        if model.model.decoder.final_layer_norm is not None:
            hidden_states = model.model.decoder.final_layer_norm(hidden_states)
        if model.model.decoder.project_out is not None:
            hidden_states = model.model.decoder.project_out(hidden_states)
        lm_logits = model.lm_head(hidden_states)
        shift_logits = lm_logits[:, :-1, :].contiguous()
        shift_labels = testenc[:, (i * seqlen):((i + 1) * seqlen)][:, 1:]
        loss_fct = nn.CrossEntropyLoss()
        loss = loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
        neg_log_likelihood = loss.float() * seqlen
        nlls.append(neg_log_likelihood)
    ppl = torch.exp(torch.stack(nlls).sum() / (nsamples * seqlen))
    print(ppl.item())

    model.config.use_cache = use_cache

def eval(model_name, model, eval_tasks):
    import gc
    time_start = time.time()

    # ppl tasks, datasets = ['wikitext2', 'ptb', 'c4-new']
    datasets = []
    if 'wikitext2' in eval_tasks: datasets.append('wikitext2'); eval_tasks.remove('wikitext2')
    if 'ptb' in eval_tasks: datasets.append('ptb'); eval_tasks.remove('ptb')
    if 'c4' in eval_tasks: datasets.append('c4'); eval_tasks.remove('c4')
    if 'ptb-new' in eval_tasks: datasets.append('ptb-new'); eval_tasks.remove('ptb-new')
    if 'c4-new' in eval_tasks: datasets.append('c4-new'); eval_tasks.remove('c4-new')
    from datautils import get_loaders
    for dataset in datasets:
        dataloader, testloader = get_loaders(
            dataset, seed=0, model=model_name, seqlen=2048
        )
        print(dataset)
        if 'llama' in model_name:
            llama_eval(model, testloader, 'cuda:0')

    gc.collect()
    torch.cuda.empty_cache()

    if len(eval_tasks) == 0: return
    model.name_or_path = model_name
    if '70b' in model_name:
        # load in 2 gpu
        state_dict = model.state_dict()
        from transformers import AutoModelForCausalLM
        model = AutoModelForCausalLM.from_pretrained(model_name, device_map='auto', torch_dtype=torch.float16, cache_dir=CACHE_DIR)
        model.load_state_dict(state_dict)
    else:
        model = model.to('cuda:0')

    # QA tasks, datasets = ['lambada_openai', 'piqa', 'hellaswag']
    datasets = []
    if 'lambada_openai' in eval_tasks: datasets.append('lambada_openai')
    if 'piqa' in eval_tasks: datasets.append('piqa')
    if 'hellaswag' in eval_tasks: datasets.append('hellaswag')
    if len(datasets) > 0:
        from lm_eval import evaluator
        import json
        results = evaluator.simple_evaluate(
            model=model,
            model_args='use_accelerate=True',
            tasks=datasets,
            num_fewshot=0,
            batch_size=2
        )
        dumped = json.dumps(results, indent=2)
        print('QA eval:')
        print(dumped)

    gc.collect()
    torch.cuda.empty_cache()

    # MMLU tasks
    if 'mmlu' not in eval_tasks: return
    import json
    from lm_eval import tasks, evaluator, utils
    mmlu_tasks = utils.pattern_match('hendrycksTest-*'.split(","), tasks.ALL_TASKS)
    print(f"Selected Tasks: {mmlu_tasks}")

    results = evaluator.simple_evaluate(
        model=model,
        model_args='use_accelerate=True',
        tasks=mmlu_tasks,
        num_fewshot=5,
        batch_size=(1 if '70b' in model_name else 2)
    )

    print(evaluator.make_table(results))

    acc_sum = 0
    count = 0
    for key in results['results']:
        if 'hendrycksTest' in key:
            acc_sum += results['results'][key]['acc']
            count += 1
    print("Num of tests", count)
    avg_acc = acc_sum / count
    print("mmlu-acc:", avg_acc)
    print('eval time: %fh' % ((time.time() - time_start) / 60. / 60.))


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', default='meta-llama/Llama-2-7b-hf', type=str) # quant base model
    parser.add_argument('--bits', default=4, choices=[2,3,4], type=int) # 3bit fp/nf/af todo
    parser.add_argument('--format', default='int', choices=['int', 'fp', 'nf']) # quantize model to int / fp / nf
    parser.add_argument('--group_size', default=-1, type=int) # it is recommended to set the value to 128
    parser.add_argument('--gptq_quant', action='store_true') # use gptq or not, af quant can not use gptq currently (calculate hessian etc)
    parser.add_argument('--two_scale', action='store_true') # use 2-scale for nf4
    parser.add_argument('--no_quant', action='store_true') # quant or only load&eval ori fp16 model
    parser.add_argument('--no_pack', action='store_true') # If only quant & eval in fp16, no pack. If need to save 4bit model, pack
    parser.add_argument('--tasks', default='wikitext2', type=str) # all: wikitext2,ptb,c4,hellaswag,mmlu
    args = parser.parse_args()

    quantize_config = BaseQuantizeConfig(
        bits=args.bits,
        format=args.format,
        group_size=args.group_size,
        gptq_quant=args.gptq_quant,
        two_scale=args.two_scale,
    )

    # load un-quantized model, the model will always be force loaded into cpu
    model = AutoGPTQForCausalLM.from_pretrained(args.model, quantize_config, torch_dtype=torch.float16, cache_dir=CACHE_DIR)

    # quantize model
    if not args.no_quant:
        logger.info(f'Base model: {args.model}, Format: {args.format}{args.bits}, Group_size: {args.group_size}, GPTQ: {args.gptq_quant}, Two_scale: {args.two_scale}')
        time_start = time.time()
        traindataset,testenc = get_wikitext2(128, 0, 2048, args.model)
        model.quantize(traindataset, use_triton=False, pack=(not args.no_pack))
        logger.info('quant time: %fh' % ((time.time() - time_start) / 60. / 60.))

    # save & load quantized model in fp16
    # model.model.save_pretrained('save/llama2_7b_fp4_fp16')
    # from transformers import AutoModelForCausalLM
    # model = AutoModelForCausalLM.from_pretrained("save/llama2_7b_fp4_fp16", torch_dtype=torch.float16)

    # save & load quantized model in 4bit
    # model.save_quantized('save/llama2_7b_fp4_g128')
    # model = AutoGPTQForCausalLM.from_quantized('save/llama2_7b_fp4_g128', device="cuda:0", use_triton=False, 
    #     inject_fused_attention=False, inject_fused_mlp=False
    # )

    # eval
    if args.tasks == 'all': tasks = ['wikitext2', 'ptb', 'c4', 'hellaswag', 'mmlu']
    else: tasks = args.tasks.split(',')
    logger.info(f'Eval tasks: {tasks}')
    eval(args.model, model.model, tasks)

if __name__ == "__main__":
    import logging

    logging.basicConfig(
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s", level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S"
    )

    main()
