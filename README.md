Command
```
python blink/main_dense.py --faiss_index hnsw --index_path //private/home/belindali/pretrain/BLINK-mentions/models/faiss_hnsw_index.pkl --fast --test_mentions /checkpoint/belindali/entity_link/data/EL4QA_data/graphquestions_EL/test.jsonl --mention_classifier qa --mention_classifier_threshold 0.0
```

80 CPUs Running
```
srun --gpus-per-node=0 --partition=priority --comment=leaving0911 --time=3000 --cpus-per-task 80 --mem=400000 --pty -l python blink/main_dense.py --faiss_index hnsw --index_path //private/home/belindali/pretrain/BLINK-mentions/models/faiss_hnsw_index.pkl --fast --test_mentions /checkpoint/belindali/entity_link/data/EL4QA_data/graphquestions_EL/test.jsonl --mention_classifier qa --mention_classifier_threshold 0.0
```
