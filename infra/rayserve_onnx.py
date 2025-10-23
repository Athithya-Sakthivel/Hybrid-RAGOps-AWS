from __future__ import annotations
import os,sys,json,time
from typing import List,Dict,Any,Optional
import numpy as np
import ray
from ray import serve
from transformers import PreTrainedTokenizerFast
ONNX_USE_CUDA=os.getenv("ONNX_USE_CUDA","false").lower() in ("1","true","yes")
MODEL_DIR_EMBED="/models/gte-modernbert-base"
MODEL_DIR_RERANK="/models/gte-reranker-modernbert-base"
ONNX_EMBED_NAME=os.getenv("ONNX_EMBED_NAME","model_int8.onnx")
ONNX_RERANK_NAME=os.getenv("ONNX_RERANK_NAME","model_int8.onnx")
EMBED_REPLICAS=int(os.getenv("EMBED_REPLICAS","1"))
RERANK_REPLICAS=int(os.getenv("RERANK_REPLICAS","1"))
EMBED_GPU=1 if ONNX_USE_CUDA and os.getenv("EMBED_GPU_PER_REPLICA","0")!="0" else 0
RERANK_GPU=1 if ONNX_USE_CUDA and os.getenv("RERANK_GPU_PER_REPLICA","0")!="0" else 0
MAX_RERANK=int(os.getenv("MAX_RERANK","256"))
try:
    import onnxruntime as ort
except Exception as e:
    raise ImportError("onnxruntime not importable. Install onnxruntime or onnxruntime-gpu. "+str(e))
if ONNX_USE_CUDA:
    providers_avail=ort.get_available_providers()
    if "CUDAExecutionProvider" not in providers_avail:
        raise RuntimeError("ONNX_USE_CUDA=true but CUDAExecutionProvider not available in onnxruntime providers: "+str(providers_avail))
def make_session(path: str):
    so=ort.SessionOptions()
    so.intra_op_num_threads=int(os.getenv("ORT_INTRA_THREADS","1"))
    so.inter_op_num_threads=int(os.getenv("ORT_INTER_THREADS","1"))
    providers=["CUDAExecutionProvider","CPUExecutionProvider"] if ONNX_USE_CUDA else ["CPUExecutionProvider"]
    sess=ort.InferenceSession(path,sess_options=so,providers=providers)
    return sess
def mean_pool(last_hidden: np.ndarray, attention_mask: np.ndarray) -> np.ndarray:
    mask=attention_mask.astype(np.float32)
    mask=mask[:,:,None]
    summed=(last_hidden*mask).sum(axis=1)
    denom=np.maximum(mask.sum(axis=1),1e-9)
    return summed/denom
def _ensure_file(path: str):
    if not os.path.exists(path):
        raise FileNotFoundError(path)
@serve.deployment(name="embed_onxx",num_replicas=EMBED_REPLICAS,ray_actor_options={"num_gpus":EMBED_GPU})
class ONNXEmbed:
    def __init__(self,model_dir: str = MODEL_DIR_EMBED,onnx_name: str = ONNX_EMBED_NAME):
        tokenizer_file=os.path.join(model_dir,"tokenizer.json")
        _ensure_file(tokenizer_file)
        self.tokenizer=PreTrainedTokenizerFast(tokenizer_file=tokenizer_file)
        if getattr(self.tokenizer,"pad_token",None) is None:
            if getattr(self.tokenizer,"eos_token",None) is not None:
                self.tokenizer.pad_token=self.tokenizer.eos_token
            elif getattr(self.tokenizer,"sep_token",None) is not None:
                self.tokenizer.pad_token=self.tokenizer.sep_token
            else:
                self.tokenizer.add_special_tokens({"pad_token":"[PAD]"})
        onnx_path=os.path.join(model_dir,"onnx",onnx_name)
        _ensure_file(onnx_path)
        self.sess=make_session(onnx_path)
        self.input_names=[inp.name for inp in self.sess.get_inputs()]
        self.output_names=[out.name for out in self.sess.get_outputs()]
        try:
            toks=self.tokenizer(["test"],padding=True,truncation=True,return_tensors="np",max_length=8)
            ort_inputs={}
            for k,v in toks.items():
                if k in self.input_names:
                    ort_inputs[k]=v.astype(np.int64)
            outputs=self.sess.run(None,ort_inputs)
            out=None
            for arr in outputs:
                arr=np.asarray(arr)
                if arr.ndim==3:
                    attn=toks.get("attention_mask",np.ones(arr.shape[:2],dtype=np.int64))
                    out=mean_pool(arr,attn)
                    break
                if arr.ndim==2:
                    out=arr
                    break
            if out is None:
                last=np.asarray(outputs[-1])
                if last.ndim>2:
                    out=last.reshape((last.shape[0],-1))
                else:
                    out=last
            if out is None or out.ndim!=2:
                raise RuntimeError("Unable to infer embedding output shape from ONNX outputs.")
            self._embed_dim=int(out.shape[1])
            print(f"[embed_onxx] startup OK: detected embed_dim={self._embed_dim}")
        except Exception as e:
            print("[embed_onxx] startup check failed:",e)
            raise
    async def __call__(self,request):
        body=await request.json()
        texts=body.get("texts",[])
        if not isinstance(texts,list):
            texts=[texts]
        toks=self.tokenizer(texts,padding=True,truncation=True,return_tensors="np",max_length=512)
        assert isinstance(toks,dict) and 'input_ids' in toks and 'attention_mask' in toks, f"tokenizer output invalid: {list(toks.keys())}"
        batch=toks['input_ids'].shape[0]
        ort_inputs: Dict[str,Any]={}
        for k,v in toks.items():
            if k in self.input_names:
                ort_inputs[k]=v.astype(np.int64)
            else:
                if k=="input_ids":
                    for cand in ("input_ids","input","input.1"):
                        if cand in self.input_names:
                            ort_inputs[cand]=v.astype(np.int64)
                            break
        outputs=self.sess.run(None,ort_inputs)
        found=False
        vecs: Optional[np.ndarray]=None
        for arr in outputs:
            arr=np.asarray(arr)
            if arr.ndim==3 and arr.shape[0]==batch:
                attn=toks.get("attention_mask",np.ones(arr.shape[:2],dtype=np.int64))
                vecs=mean_pool(arr,attn)
                found=True
                break
            if arr.ndim==2 and arr.shape[0]==batch:
                vecs=arr
                found=True
                break
        if not found:
            last=np.asarray(outputs[-1])
            if last.ndim>2 and last.shape[0]==batch:
                vecs=last.reshape((last.shape[0],-1))
                found=True
        if not found or vecs is None:
            raise RuntimeError(f"ONNX embed outputs invalid shapes; outputs shapes {[np.asarray(o).shape for o in outputs]} batch={batch}")
        norms=np.linalg.norm(vecs,axis=1,keepdims=True)
        norms=np.maximum(norms,1e-12)
        vecs=(vecs/norms).astype(float)
        if vecs.ndim!=2:
            raise RuntimeError(f"embed vectors ndim != 2: {vecs.ndim}")
        if vecs.shape[1]!=self._embed_dim:
            raise RuntimeError(f"embed dim mismatch {vecs.shape[1]} != detected {self._embed_dim}")
        return {"vectors":[v.tolist() for v in vecs]}
@serve.deployment(name="rerank_onxx",num_replicas=RERANK_REPLICAS,ray_actor_options={"num_gpus":RERANK_GPU})
class ONNXRerank:
    def __init__(self,model_dir: str = MODEL_DIR_RERANK,onnx_name: str = ONNX_RERANK_NAME):
        tokenizer_file=os.path.join(model_dir,"tokenizer.json")
        _ensure_file(tokenizer_file)
        self.tokenizer=PreTrainedTokenizerFast(tokenizer_file=tokenizer_file)
        if getattr(self.tokenizer,"pad_token",None) is None:
            if getattr(self.tokenizer,"eos_token",None) is not None:
                self.tokenizer.pad_token=self.tokenizer.eos_token
            elif getattr(self.tokenizer,"sep_token",None) is not None:
                self.tokenizer.pad_token=self.tokenizer.sep_token
            else:
                self.tokenizer.add_special_tokens({"pad_token":"[PAD]"})
        onnx_path=os.path.join(model_dir,"onnx",onnx_name)
        _ensure_file(onnx_path)
        self.sess=make_session(onnx_path)
        self.input_names=[inp.name for inp in self.sess.get_inputs()]
        self.output_names=[out.name for out in self.sess.get_outputs()]
        try:
            toks=self.tokenizer([("q","a"),("q","b")],padding=True,truncation=True,return_tensors="np",max_length=8)
            ort_inputs={}
            for k,v in toks.items():
                if k in self.input_names:
                    ort_inputs[k]=v.astype(np.int64)
                else:
                    if k=="input_ids":
                        for cand in ("input_ids","input","input.1"):
                            if cand in self.input_names:
                                ort_inputs[cand]=v.astype(np.int64)
                                break
            outs=self.sess.run(None,ort_inputs)
            derived=None
            for arr in outs:
                arr=np.asarray(arr)
                if arr.ndim==1 and arr.shape[0]==2:
                    derived=arr;break
                if arr.ndim==2 and arr.shape[0]==2:
                    derived=arr[:,0];break
            if derived is None:
                derived=np.asarray(outs[-1]).reshape(2,-1)[:,0]
            print(f"[rerank_onxx] startup OK: sample scores shape {derived.shape}")
        except Exception as e:
            print("[rerank_onxx] startup check failed:",e)
            raise
    async def __call__(self,request):
        body=await request.json()
        q=body.get("query","")
        cands=body.get("cands",[])[:MAX_RERANK]
        if not isinstance(cands,list):
            cands=[cands]
        if len(cands)==0:
            return {"scores":[]}
        toks=self.tokenizer([(q,t) for t in cands],padding=True,truncation=True,return_tensors="np",max_length=512)
        ort_inputs: Dict[str,Any]={}
        for k,v in toks.items():
            if k in self.input_names:
                ort_inputs[k]=v.astype(np.int64)
            else:
                if k=="input_ids":
                    for cand in ("input_ids","input","input.1"):
                        if cand in self.input_names:
                            ort_inputs[cand]=v.astype(np.int64)
                            break
        outputs=self.sess.run(None,ort_inputs)
        scores=None
        for arr in outputs:
            arr=np.asarray(arr)
            if arr.ndim==1 and arr.shape[0]==len(cands):
                scores=arr;break
            if arr.ndim==2 and arr.shape[0]==len(cands):
                scores=arr[:,0];break
        if scores is None:
            last=np.asarray(outputs[-1])
            try:
                scores=last.reshape(len(cands),-1)[:,0]
            except Exception as e:
                raise RuntimeError(f"unable to parse reranker outputs shapes {[np.asarray(o).shape for o in outputs]}: {e}")
        return {"scores":[float(s) for s in np.asarray(scores).astype(float)]}
def main():
    ray.init(address=os.getenv("RAY_ADDRESS",None))
    serve.start(detached=True)
    serve.run(ONNXEmbed.bind(),ONNXRerank.bind())
    print("ONNX Serve up: embed_onxx, rerank_onxx")
if __name__=="__main__":
    main()
