# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
import argparse
import json
import sys
import faiss

from tqdm import tqdm
import logging
import torch
import numpy as np
from colorama import init
from termcolor import colored
import torch.nn.functional as F

import blink.ner as NER
from torch.utils.data import DataLoader, SequentialSampler, TensorDataset
from blink.biencoder.biencoder import BiEncoderRanker, load_biencoder, to_bert_input
from blink.biencoder.data_process import (
    process_mention_data,
    get_context_representation_single_mention,
    get_candidate_representation,
)
import blink.candidate_ranking.utils as utils
import math

import blink.vcg_utils
from blink.vcg_utils.mention_extraction import extract_entities
from blink.vcg_utils.measures import entity_linking_tp_with_overlap

import os
import sys
from tqdm import tqdm
import pdb
import time


HIGHLIGHTS = [
    "on_red",
    "on_green",
    "on_yellow",
    "on_blue",
    "on_magenta",
    "on_cyan",
]


def _print_colorful_text(input_sentence, samples):
    init()  # colorful output
    msg = ""
    if samples and (len(samples) > 0):
        msg += input_sentence[0 : int(samples[0]["start_pos"])]
        for idx, sample in enumerate(samples):
            msg += colored(
                input_sentence[int(sample["start_pos"]) : int(sample["end_pos"])],
                "grey",
                HIGHLIGHTS[idx % len(HIGHLIGHTS)],
            )
            if idx < len(samples) - 1:
                msg += input_sentence[
                    int(sample["end_pos"]) : int(samples[idx + 1]["start_pos"])
                ]
            else:
                msg += input_sentence[int(sample["end_pos"]) : ]
    else:
        msg = input_sentence
    print("\n" + str(msg) + "\n")


def _print_colorful_prediction(idx, sample, e_id, e_title, e_text, e_url, show_url=False):	
    print(colored(sample["mention"], "grey", HIGHLIGHTS[idx % len(HIGHLIGHTS)]))	
    to_print = "id:{}\ntitle:{}\ntext:{}\n".format(e_id, e_title, e_text[:256])	
    if show_url:	
        to_print += "url:{}\n".format(e_url)	
    print(to_print)


def _annotate(ner_model, input_sentences):
    ner_output_data = ner_model.predict(input_sentences)
    sentences = ner_output_data["sentences"]
    mentions = ner_output_data["mentions"]
    samples = []
    for mention in mentions:
        record = {}
        record["label"] = "unknown"
        record["label_id"] = -1
        # LOWERCASE EVERYTHING !
        record["context_left"] = sentences[mention["sent_idx"]][
            : mention["start_pos"]
        ].lower()
        record["context_right"] = sentences[mention["sent_idx"]][
            mention["end_pos"] :
        ].lower()
        record["mention"] = mention["text"].lower()
        record["start_pos"] = int(mention["start_pos"])
        record["end_pos"] = int(mention["end_pos"])
        record["sent_idx"] = mention["sent_idx"]
        samples.append(record)
    return samples


def _load_candidates(
    entity_catalogue, entity_encoding, entity_token_ids, biencoder, max_seq_length,
    get_kbids=True, logger=None,
):
    candidate_encoding = torch.load(entity_encoding)
    candidate_token_ids = torch.load(entity_token_ids)
    if os.path.exists("models/title2id.json"):
        title2id = json.load(open("models/title2id.json"))
        id2title = json.load(open("models/id2title.json"))
        logger.info("Loaded titles")
        id2text = json.load(open("models/id2text.json"))
        logger.info("Loaded texts")
        kb2id = json.load(open("models/kb2id.json"))
        id2kb = json.load(open("models/id2kb.json"))
        logger.info("Loaded KBIDS")
        wikipedia_id2local_id = json.load(open("models/wikipedia_id2local_id.json"))
        return candidate_encoding, candidate_token_ids, title2id, id2title, id2text, wikipedia_id2local_id, kb2id, id2kb

    # load all the 5903527 entities
    title2id = {}
    id2title = {}
    id2text = {}
    kb2id = {}
    id2kb = {}
    resave_encodings = False
    bsz = 128
    candidate_rep = []

    wikipedia_id2local_id = {}
    local_idx = 0
    missing_entity_ids = 0
    with open(entity_catalogue, "r") as fin:
        lines = fin.readlines()
        for i, line in enumerate(tqdm(lines)):
            entity = json.loads(line)
            if "idx" in entity:
                split = entity["idx"].split("curid=")
                if len(split) > 1:
                    wikipedia_id = int(split[-1].strip())
                else:
                    wikipedia_id = entity["idx"].strip()

                assert wikipedia_id not in wikipedia_id2local_id
                wikipedia_id2local_id[wikipedia_id] = local_idx

            title2id[entity["title"]] = local_idx
            if get_kbids:
                if "kb_idx" in entity:
                    kb2id[entity["kb_idx"]] = local_idx
                    id2kb[local_idx] = entity["kb_idx"]
                else:
                    missing_entity_ids += 1
            id2title[local_idx] = entity["title"]
            id2text[local_idx] = entity["text"]
            local_idx += 1

            if i > len(candidate_encoding):
                resave_encodings = True
                # not in candidate encodings file, generate through forward method
                candidate_rep.append(get_candidate_representation(
                    # entity["title"] + " "
                    entity["text"].strip(), biencoder.tokenizer,
                    128, entity["title"].strip()
                )['ids'])
                if len(candidate_rep) == bsz or i == len(lines) - 1:
                    try:
                        curr_cand_encs = biencoder.encode_candidate(
                            torch.LongTensor(candidate_rep)
                        )
                        with open("models/entities_with_ids.txt", "a") as f:
                            d=f.write(json.dumps(curr_cand_encs.tolist()) + "\n")
                    except RuntimeError:
                        import pdb
                        pdb.set_trace()
                    candidate_rep = []

    if resave_encodings:
        torch.save(candidate_encoding, "new_" + entity_encoding)
    if logger:
        logger.info("missing {}/{} wikidata IDs".format(missing_entity_ids, local_idx))

    json.dump(title2id, open("models/title2id.json", "w"))
    json.dump(id2title, open("models/id2title.json", "w"))
    json.dump(id2text, open("models/id2text.json", "w"))
    json.dump(kb2id, open("models/kb2id.json", "w"))
    json.dump(id2kb, open("models/id2kb.json", "w"))
    json.dump(wikipedia_id2local_id, open("models/wikipedia_id2local_id.json", "w"))
    return candidate_encoding, candidate_token_ids, title2id, id2title, id2text, wikipedia_id2local_id, kb2id, id2kb


def __map_test_entities(test_entities_path, title2id, logger):
    # load the 732859 tac_kbp_ref_know_base entities
    kb2id = {}
    id2kb = {}
    missing_pages = 0
    missing_entity_ids = 0
    n = 0
    with open(test_entities_path, "r") as fin:
        lines = fin.readlines()
        for line in tqdm(lines):
            entity = json.loads(line)
            if entity["title"] not in title2id:
                missing_pages += 1
            else:
                if "kb_idx" in entity:
                    kb2id[entity["kb_idx"]] = title2id[entity["title"]]
                    id2kb[title2id[entity["title"]]] = entity["kb_idx"]
                else:
                    missing_entity_ids += 1
            n += 1
    if logger:
        logger.info("missing {}/{} pages".format(missing_pages, n))
        logger.info("missing {}/{} wikidata IDs".format(missing_entity_ids, n))
    return kb2id, id2kb


def get_mention_bound_candidates(
    do_ner, record, new_record,
    saved_ngrams=None, ner_model=None, max_mention_len=4,
    ner_errors=0, sample_idx=0, qa_classifier_saved=None,
    biencoder=None,
):
    if do_ner == "ngram":
        if record["utterance"] not in saved_ngrams:
            tagged_text = vcg_utils.utils.get_tagged_from_server(record["utterance"], caseless=True)
            # get annotated entities for each sample
            samples = []
            for mention_len in range(min(len(tagged_text), max_mention_len), 0, -1):
                annotated_sample = extract_entities(tagged_text, ngram_len=mention_len)
                samples += annotated_sample
            saved_ngrams[record["utterance"]] = samples
        else:
            samples = saved_ngrams[record["utterance"]]
        # setup new_record
        for sample in samples:
            sample["context_left"] = record['utterance'][:sample['offsets'][0]]
            sample["context_right"] = record['utterance'][sample['offsets'][1]:]
            sample["mention"] = record['utterance'][sample['offsets'][0]:sample['offsets'][1]]

    elif do_ner == "flair":
        # capitalize each word
        sentences = [record["utterance"].title()]
        samples = _annotate(ner_model, sentences)
        if len(samples) == 0:
            ner_errors += 1
            # sample_to_all_context_inputs.append([])
            return None, None, saved_ngrams, sample_idx

    elif do_ner == "single" or do_ner == "joint":
        # assume single mention boundary
        samples = [{
            "context_left": "",
            "context_right": "",
            "mention": record["utterance"],
        }]

    elif do_ner == "qa_classifier":
        samples = []
        for _q in qa_classifier_saved[record['question_id']]:
            # TODO read from file
            samples.append({
                "context_left": _q['passage'].split('<answer>')[0],
                "context_right": _q['passage'].split('</answer>')[1],
                "mention": _q['text'],
            })

    new_record_list = []
    sample_idx_list = []
    if do_ner != "none":
        for sample in samples:
            new_record_list.append({
                "q_id": new_record["q_id"],
                "label": new_record["label"],
                "label_id": new_record["label_id"],
                "context_left": sample["context_left"].lower(),
                "context_right": sample["context_right"].lower(),
                "mention": sample["mention"].lower(),
            })
            if "all_gold_entities" in new_record:
                new_record_list[len(new_record_list)-1]["all_gold_entities"] = new_record["all_gold_entities"]
                new_record_list[len(new_record_list)-1]["all_gold_entities_ids"] = new_record["all_gold_entities_ids"]
                new_record_list[len(new_record_list)-1]["all_gold_entities_pos"] = record['entities_pos']
            if "main_entity_pos" in record:
                new_record_list[len(new_record_list)-1]["gold_context_left"] = record[
                    'utterance'][:record['main_entity_pos'][0]].lower()
                new_record_list[len(new_record_list)-1]["gold_context_right"] = record[
                    'utterance'][record['main_entity_pos'][1]:].lower()
                new_record_list[len(new_record_list)-1]["gold_mention"] = record[
                    'utterance'][record['main_entity_pos'][0]:record['main_entity_pos'][1]].lower()
            sample_idx_list.append(sample_idx)
            sample_idx += 1
        if len(samples) == 0:
            # found no samples for this record...
            new_record_list.append({
                "q_id": new_record["q_id"],
                "label": new_record["label"],
                "label_id": new_record["label_id"],
                "context_left": "", "context_right": "", "mention": "",
            })
            if "all_gold_entities" in new_record:
                new_record_list[len(new_record_list)-1]["all_gold_entities"] = new_record["all_gold_entities"]
                new_record_list[len(new_record_list)-1]["all_gold_entities_ids"] = new_record["all_gold_entities_ids"]
                new_record_list[len(new_record_list)-1]["all_gold_entities_pos"] = record['entities_pos']
            if "main_entity_pos" in record:
                new_record_list[len(new_record_list)-1]["gold_context_left"] = record[
                    'utterance'][:record['main_entity_pos'][0]].lower()
                new_record_list[len(new_record_list)-1]["gold_context_right"] = record[
                    'utterance'][record['main_entity_pos'][1]:].lower()
                new_record_list[len(new_record_list)-1]["gold_mention"] = record[
                    'utterance'][record['main_entity_pos'][0]:record['main_entity_pos'][1]].lower()
            sample_idx_list.append(sample_idx)
            sample_idx += 1
    else:
        # entity bounds are given
        entity_bounds = record["main_entity_pos"]
        new_record["context_left"] = record["utterance"][:entity_bounds[0]].lower()
        new_record["gold_context_left"] = new_record["context_left"].lower()
        new_record["context_right"] = record["utterance"][entity_bounds[1]:].lower()
        new_record["gold_context_right"] = new_record["context_right"].lower()
        new_record["mention"] = record["main_entity_tokens"].lower()
        new_record["gold_meniton"] = new_record["mention"].lower()
        if "all_gold_entities" in new_record:
            new_record["all_gold_entities"] = new_record["all_gold_entities"]
            new_record["all_gold_entities_ids"] = new_record["all_gold_entities_ids"]
        new_record_list = [new_record]
        sample_idx_list = [sample_idx]
        sample_idx += 1

    return new_record_list, sample_idx_list, saved_ngrams, ner_errors, sample_idx


def __load_test(
    test_filename, kb2id, logger, args,
    qa_data=False, id2kb=None, title2id=None,
    do_ner="none", use_ngram_extractor=False, max_mention_len=4,
    debug=False, main_entity_only=False, biencoder=None,
):
    test_samples = []
    sample_to_all_context_inputs = []  # if multiple mentions found for an example, will have multiple inputs
                                        # maps indices of examples to list of all indices in `samples`
                                        # i.e. [[0], [1], [2, 3], ...]
    unknown_entity_samples = []
    num_unknown_entity_samples = 0
    num_no_gold_entity = 0
    ner_errors = 0
    ner_model = None
    if do_ner == "flair":
        # Load NER model
        ner_model = NER.get_model()

    saved_ngrams = {}
    if do_ner == "ngram":
        save_file = "{}_saved_ngrams_new_rules_{}.json".format(test_filename, max_mention_len)
        if os.path.exists(save_file):
            saved_ngrams = json.load(open(save_file))

    qa_classifier_saved = {}
    if do_ner == "qa_classifier":
        assert getattr(args, 'mention_classifier_threshold', None) is not None
        if args.mention_classifier_threshold == "top1":
            do_top_1 = True
        else:
            do_top_1 = False
            mention_classifier_threshold = float(args.mention_classifier_threshold)
        if "webqsp.test" in test_filename:
            test_predictions_json = "/private/home/sviyer/datasets/webqsp/test_predictions.json"
        elif "webqsp.dev" in test_filename:
            test_predictions_json = "/private/home/sviyer/datasets/webqsp/dev_predictions.json"
        elif "graph.test" in test_filename:
            test_predictions_json = "/private/home/sviyer/datasets/graphquestions/test_predictions.json"
        elif "nq_dev" in test_filename:
            test_predictions_json = "/private/home/sviyer/datasets/nq/dev_predictions.json"
        with open(test_predictions_json) as f:
            for line in f:
                line_json = json.loads(line)
                all_ex_preds = []
                for i, pred in enumerate(line_json['all_predictions']):
                    if "test" in test_filename:
                        pred['logit'][1] = math.log(pred['logit'][1])
                    if (
                        (do_top_1 and i == 0) or 
                        (not do_top_1 and pred['logit'][1] > mention_classifier_threshold)
                        # or i == 0  # have at least 1 candidate
                    ):
                        all_ex_preds.append(pred)
                assert '1' in line_json['predictions']
                qa_classifier_saved[line_json['id']] = all_ex_preds

    with open(test_filename, "r") as fin:
        if qa_data:
            lines = json.load(fin)
            sample_idx = 0
            do_setup_samples = True

            for i, record in enumerate(tqdm(lines)):
                new_record = {}
                new_record["q_id"] = record["question_id"]

                if main_entity_only:
                    if "main_entity" not in record or record["main_entity"] is None:
                        num_no_gold_entity += 1
                        new_record["label"] = None
                        new_record["label_id"] = -1
                        new_record["all_gold_entities"] = []
                        new_record["all_gold_entities_ids"] = []
                    elif record['main_entity'] in kb2id:
                        new_record["label"] = record["main_entity"]
                        new_record["label_id"] = kb2id[record['main_entity']]
                        new_record["all_gold_entities"] = [record["main_entity"]]
                        new_record["all_gold_entities_ids"] = [kb2id[record['main_entity']]]
                    else:
                        num_unknown_entity_samples += 1
                        unknown_entity_samples.append(record)
                        # sample_to_all_context_inputs.append([])
                        # TODO DELETE?
                        continue
                else:
                    new_record["label"] = None
                    new_record["label_id"] = -1
                    if "entities" not in record or record["entities"] is None or len(record["entities"]) == 0:
                        if "main_entity" not in record or record["main_entity"] is None:
                            num_no_gold_entity += 1
                            new_record["all_gold_entities"] = []
                            new_record["all_gold_entities_ids"] = []
                        else:
                            new_record["all_gold_entities"] = [record["main_entity"]]
                            new_record["all_gold_entities_ids"] = [kb2id[record['main_entity']]]
                    else:
                        new_record["all_gold_entities"] = record['entities']
                        new_record["all_gold_entities_ids"] = []
                        for ent_id in new_record["all_gold_entities"]:
                            if ent_id in kb2id:
                                new_record["all_gold_entities_ids"].append(kb2id[ent_id])
                            else:
                                num_unknown_entity_samples += 1
                                unknown_entity_samples.append(record)

                (new_record_list, sample_idx_list,
                saved_ngrams, ner_errors, sample_idx) = get_mention_bound_candidates(
                    do_ner, record, new_record,
                    saved_ngrams=saved_ngrams, ner_model=ner_model,
                    max_mention_len=max_mention_len, ner_errors=ner_errors,
                    sample_idx=sample_idx, qa_classifier_saved=qa_classifier_saved,
                    biencoder=biencoder,
                )
                if sample_idx_list is not None:
                    sample_to_all_context_inputs.append(sample_idx_list)
                if new_record_list is not None:
                    test_samples += new_record_list

        else:
            lines = fin.readlines()
            for i, line in enumerate(tqdm(lines)):
                record = json.loads(line)
                record["label"] = record["label_id"]
                record["q_id"] = record["query_id"]
                if record["label"] in kb2id:
                    sample_to_all_context_inputs.append([len(test_samples)])
                    record["label_id"] = kb2id[record["label"]]
                    # LOWERCASE EVERYTHING !
                    record["context_left"] = record["context_left"].lower()
                    record["context_right"] = record["context_right"].lower()
                    record["mention"] = record["mention"].lower()
                    record["gold_context_left"] = record["context_left"].lower()
                    record["gold_context_right"] = record["context_right"].lower()
                    record["gold_mention"] = record["mention"].lower()
                    test_samples.append(record)

    # save info and log
    with open("saved_preds/unknown.json", "w") as f:
        json.dump(unknown_entity_samples, f)
    if do_ner == "ngram":
        save_file = "{}_saved_ngrams_new_rules_{}.json".format(test_filename, max_mention_len)
        with open(save_file, "w") as f:
            json.dump(saved_ngrams, f)
        logger.info("Finished saving to {}".format(save_file))
    if logger:
        logger.info("{}/{} samples considered".format(len(sample_to_all_context_inputs), len(lines)))
        logger.info("{} samples generated".format(len(test_samples)))
        logger.info("{} samples with unknown entities considered".format(num_unknown_entity_samples))
        logger.info("{} samples with no gold entities considered".format(num_no_gold_entity))
        logger.info("ner errors: {}".format(ner_errors))
    return test_samples, num_unknown_entity_samples, sample_to_all_context_inputs


def _get_test_samples(
    test_filename, test_entities_path, title2id, kb2id, id2kb, logger,
    qa_data=False, do_ner="none", debug=False, main_entity_only=False, do_map_test_entities=True,
    biencoder=None,
):
    # TODO GET CORRECT IDS
    # if debug:
    #     test_entities_path = "/private/home/belindali/temp/BLINK-Internal/models/entity_debug.jsonl"
    if do_map_test_entities:
        kb2id, id2kb = __map_test_entities(test_entities_path, title2id, logger)
    test_samples, num_unk, sample_to_all_context_inputs = __load_test(
        test_filename, kb2id, logger, args,
        qa_data=qa_data, id2kb=id2kb, title2id=title2id,
        do_ner=do_ner, debug=debug, main_entity_only=main_entity_only,
        biencoder=biencoder,
    )
    return test_samples, kb2id, id2kb, num_unk, sample_to_all_context_inputs


def _process_biencoder_dataloader(samples, tokenizer, biencoder_params):
    tokens_data, tensor_data_tuple = process_mention_data(
        samples=samples,
        tokenizer=tokenizer,
        max_context_length=biencoder_params["max_context_length"],
        max_cand_length=biencoder_params["max_cand_length"],
        silent=False,
        logger=None,
        debug=biencoder_params["debug"],
        add_mention_bounds=(not biencoder_params.get("no_mention_bounds", False)),
        get_cached_representation=False,  # TODO???
    )
    tensor_data = TensorDataset(*tensor_data_tuple)
    sampler = SequentialSampler(tensor_data)
    dataloader = DataLoader(
        tensor_data, sampler=sampler, batch_size=biencoder_params["eval_batch_size"]
    )
    return dataloader


def _run_biencoder(
    args, biencoder, dataloader, candidate_encoding, samples,
    top_k=100, device="cpu", jointly_extract_mentions=False,
    sample_to_all_context_inputs=None, num_mentions=10,  # TODO don't hardcode
    mention_classifier_threshold=0.25,
    cand_encs_flat_index=None,
):
    # TODO DELETE THIS
    # cand_encs_npy = np.load("/private/home/belindali/BLINK/models/all_entities_large.npy")  # TODO DONT HARDCODE THESE PATHS
    # d = cand_encs_npy.shape[1]
    # nsplits = 100
    # cand_encs_flat_index = faiss.IndexFlatIP(d)
    # cand_encs_quantizer = faiss.IndexFlatIP(d)
    # assert cand_encs_quantizer.is_trained
    # cand_encs_index = faiss.IndexIVFFlat(cand_encs_quantizer, d, nsplits, faiss.METRIC_INNER_PRODUCT)
    # assert not cand_encs_index.is_trained
    # cand_encs_index.train(cand_encs_npy)  # 15s
    # assert cand_encs_index.is_trained
    # cand_encs_index.add(cand_encs_npy)  # 41s
    # cand_encs_flat_index.add(cand_encs_npy)
    # assert cand_encs_index.ntotal == cand_encs_npy.shape[0]
    # assert cand_encs_flat_index.ntotal == cand_encs_npy.shape[0]
    # cand_encs_index.nprobe = 20
    # logger.info("Built and trained FAISS index on entity encodings")
    # num_neighbors = 10
    #'''

    biencoder.model.eval()
    labels = []
    context_inputs = []
    nns = []
    dists = []
    mention_dists = []
    cand_dists = []
    sample_idx = 0
    ctxt_idx = 0
    new_samples = samples
    new_sample_to_all_context_inputs = sample_to_all_context_inputs
    if jointly_extract_mentions:
        new_samples = []
        new_sample_to_all_context_inputs = []
    for step, batch in enumerate(tqdm(dataloader)):
        context_input, _, label_ids, mention_idxs, mention_idxs_mask = batch
        with torch.no_grad():
            # # TODO DELETE THIS
            # # get mention encoding
            # embedding_context, start_logits, end_logits = biencoder.encode_context(
            #     context_input, gold_mention_idxs=mention_idxs
            # )
            # # do faiss search for closest entity
            # D, I = cand_encs_flat_index.search(embedding_context.contiguous().detach().cpu().numpy(), 1)
            # I = I.flatten()
            # assert np.all(I == scores.argmax(1).detach().cpu().numpy())
            # # '''

            if not jointly_extract_mentions:
                gold_mention_idx_mask = torch.ones(mention_idxs.size()[:2], dtype=torch.bool)
                import pdb
                pdb.set_trace()
                scores, mention_logits, mention_bounds, _, _ = biencoder.score_candidate(
                    context_input, None,
                    cand_encs=candidate_encoding.to(device),
                    gold_mention_idxs=mention_idxs.to(device),
                    gold_mention_idx_mask=gold_mention_idx_mask.to(device),
                    topK_threshold=0.15,
                )
            else:
                token_idx_ctxt, segment_idx_ctxt, mask_ctxt = to_bert_input(context_input, biencoder.NULL_IDX)
                context_encoding, _, _ = biencoder.model.context_encoder.bert_model(
                    token_idx_ctxt, segment_idx_ctxt, mask_ctxt,  # what is segment IDs?
                )
                mention_logits, mention_bounds, _, _ = biencoder.model.classification_heads['mention_scores'](context_encoding, mask_ctxt)

                # DIM (num_total_mentions, embed_dim)
                # mention_pos = (torch.sigmoid(mention_logits) >= mention_classifier_threshold).nonzero()
                # start_time = time.time()
                # mention_pos = (torch.sigmoid(mention_logits) >= 0.2).nonzero()
                # end_time = time.time()
                top_mention_logits, mention_pos_2 = mention_logits.topk(top_k)
                # 2nd part of OR for if nothing is > 0
                mention_pos_2 = torch.stack([torch.arange(mention_pos_2.size(0)).unsqueeze(-1).expand_as(mention_pos_2), mention_pos_2], dim=-1)
                mention_pos_2_mask = top_mention_logits >= 0
                # mention_pos_2_mask = torch.sigmoid(top_mention_logits) >= mention_classifier_threshold
                # [overall mentions, 2]
                mention_pos_2 = mention_pos_2[mention_pos_2_mask | (mention_pos_2_mask.sum(1) == 0).unsqueeze(-1)]
                mention_pos_2 = mention_pos_2.view(-1, 2)
                # end_time_2 = time.time()
                # print(end_time - start_time)
                # print(end_time_2 - end_time)
                # tuples of (instance in batch, mention id) of what to include
                mention_pos = mention_pos_2
                # TODO MAYBE TOP K HERE??
                # '''

                # mention_pos_mask = torch.sigmoid(mention_logits) > 0.25
                # reshape back to (bs, num_mentions) mask
                mention_pos_mask = torch.zeros(mention_logits.size(), dtype=torch.bool).to(mention_pos.device)
                mention_pos_mask[mention_pos_2[:,0], mention_pos_2[:,1]] = 1
                # (bs, num_mentions, 2)
                mention_idxs = mention_bounds.clone()
                mention_idxs[~mention_pos_mask] = 0

                # take highest scoring mention
                # (bs, num_mentions, embed_dim)
                embedding_ctxt = biencoder.model.classification_heads['get_context_embeds'](context_encoding, mention_idxs)
                
                # (num_total_mentions, embed_dim)
                embedding_ctxt = embedding_ctxt[mention_pos_mask]
                # (num_total_mentions, 2)
                mention_idxs = mention_idxs[mention_pos_mask]

                # DIM (num_total_mentions, num_candidates)
                # TODO search for topK entities with FAISS
                # start_time = time.time()
                if embedding_ctxt.size(0) > 1:
                    cand_scores = embedding_ctxt.squeeze(0).mm(candidate_encoding.to(device).t())
                else:
                    cand_scores = embedding_ctxt.mm(candidate_encoding.to(device).t())
                # end_time = time.time()
                # cand_scores = torch.log_softmax(cand_scores, 1)
                softmax_time = time.time()
                cand_dist, cand_indices = cand_scores.topk(10)  # TODO DELETE
                top_time = time.time()
                cand_scores = torch.log_softmax(cand_dist, 1)
                # back into (num_total_mentions, num_candidates)
                soft_time = time.time()
                # cand_scores_reconstruct = torch.ones(embedding_ctxt.size(0), candidate_encoding.size(0), dtype=cand_scores.dtype).to(cand_scores.device) * -float("inf")
                # # # DIM (bs, max_pred_mentions, num_candidates)
                # cand_scores_reconstruct[torch.arange(cand_scores_reconstruct.size(0)).unsqueeze(-1), cand_indices] = cand_scores
                # reconstruct_time = time.time()
                # cand_scores = cand_scores_reconstruct
                # print(top_time - softmax_time)
                # print(soft_time - top_time)
                # print(reconstruct_time - soft_time)
                # reconstruct_time = time.time()
                # print(softmax_time - end_time)
                # print(reconstruct_time - softmax_time)

                # scores = F.log_softmax(cand_scores, dim=-1)
                # softmax_time = time.time()
                # cand_scores, _ = cand_encs_flat_index.search(embedding_ctxt.contiguous().detach().cpu().numpy(), top_k)
                # cand_scores = torch.tensor(cand_scores)
                # # reconstruct cand_scores to (bs, num_candidates)
                # faiss_time = time.time()
                # print(end_time - start_time)
                # print(softmax_time - end_time)
                # print(faiss_time - softmax_time)
                # DIM (num_total_mentions, num_candidates)
                if args.final_thresholding != "top_entity_by_mention":
                    # DIM (num_total_mentions, num_candidates)
                    # log p(entity && mb) = log [p(entity|mention bounds) * p(mention bounds)] = log p(e|mb) + log p(mb)
                    # scores += torch.sigmoid(mention_logits)[mention_pos_mask].unsqueeze(-1)
                    mention_scores = mention_logits[mention_pos_mask].unsqueeze(-1)
                # DIM (num_total_mentions, num_candidates)
                # TODO VARIOUS SCORES OPTIONS
                # scores = torch.log_softmax(cand_scores, 1) + torch.sigmoid(mention_scores)
                # scores = cand_scores + torch.sigmoid(mention_scores)
                scores = cand_scores + mention_scores
                # import pdb
                # pdb.set_trace()
                mention_scores = mention_scores.expand_as(cand_scores)

                # # DIM (bs, max_pred_mentions, num_candidates)
                # scores_reconstruct = torch.zeros(mention_pos_mask.size(0), mention_pos_mask.sum(1).max(), scores.size(-1), dtype=scores.dtype).to(scores.device)
                # # DIM (bs, max_pred_mentions)
                mention_pos_mask_reconstruct = torch.zeros(mention_pos_mask.size(0), mention_pos_mask.sum(1).max()).bool().to(mention_pos_mask.device)
                for i in range(mention_pos_mask_reconstruct.size(1)):
                    mention_pos_mask_reconstruct[:, i] = i < mention_pos_mask.sum(1)
                # # DIM (bs, max_pred_mentions, num_candidates)
                # scores_reconstruct[mention_pos_mask_reconstruct] = scores
                # scores = scores_reconstruct

                # # DIM (bs, max_pred_mentions, num_candidates)
                # cand_scores_reconstruct = torch.zeros(mention_pos_mask.size(0), mention_pos_mask.sum(1).max(), scores.size(-1), dtype=scores.dtype).to(scores.device)
                # # DIM (bs, max_pred_mentions, num_candidates)
                # cand_scores_reconstruct[mention_pos_mask_reconstruct] = cand_scores
                # cand_scores = cand_scores_reconstruct

                # DIM (bs, max_pred_mentions, 2)
                chosen_mention_bounds = torch.zeros(mention_pos_mask.size(0),  mention_pos_mask.sum(1).max(), 2, dtype=mention_idxs.dtype).to(mention_idxs.device)
                chosen_mention_bounds[mention_pos_mask_reconstruct] = mention_idxs

                # mention_idxs = mention_idxs.view(topK_mention_bounds.size(0), topK_mention_bounds.size(1), 2)
                # assert (mention_idxs == topK_mention_bounds).all()

                # DIM (total_num_mentions, num_cands)
                # scores = scores[mention_pos_mask_reconstruct]
                # cand_scores = cand_scores[mention_pos_mask_reconstruct]
                # mention_scores = scores - 0.4 * cand_scores - 0.9  # project to dimension of scores

                # expand labels
                # DIM (total_num_mentions, 1)
                label_ids = label_ids.expand(chosen_mention_bounds.size(0), chosen_mention_bounds.size(1))[mention_pos_mask_reconstruct].unsqueeze(-1)

        if jointly_extract_mentions:
            for i, instance in enumerate(chosen_mention_bounds):
                new_sample_to_all_context_inputs.append([])
                # if len(chosen_mention_bounds[i][mention_pos_mask_reconstruct[i]]) == 0:
                #     new_samples.append({})
                #     for key in samples[sample_idx]:
                #         if key != "context_left" and key != "context_right" and key != "mention":
                #             new_samples[ctxt_idx][key] = samples[sample_idx][key]
                for j, mention_bound in enumerate(chosen_mention_bounds[i][mention_pos_mask_reconstruct[i]]):
                    new_sample_to_all_context_inputs[sample_idx].append(ctxt_idx)
                    context_left = _decode_tokens(biencoder.tokenizer, context_input[i].tolist()[:mention_bound[0]]) + " "
                    context_right = " " + _decode_tokens(biencoder.tokenizer, context_input[i].tolist()[mention_bound[1] + 1:])  # mention bound is inclusive
                    mention = _decode_tokens(
                        biencoder.tokenizer,
                        context_input[i].tolist()[mention_bound[0]:mention_bound[1] + 1]
                    )
                    new_samples.append({
                        "context_left": context_left,
                        "context_right": context_right,
                        "mention": mention,
                    })
                    for key in samples[sample_idx]:
                        if key != "context_left" and key != "context_right" and key != "mention":
                            new_samples[ctxt_idx][key] = samples[sample_idx][key]
                    ctxt_idx += 1
                sample_idx += 1

        dist, indices = scores.sort(descending=True)
        # cand_indices[i, indices[i]]
        indices = torch.gather(cand_indices, 1, indices)
        # cand_dist, cand_indices = cand_scores.topk(10)
        labels.extend(label_ids.data.numpy())
        context_inputs.extend(context_input.data.numpy())
        nns.extend(indices.data.cpu().numpy())
        dists.extend(dist.data.cpu().numpy())
        cand_dists.extend(cand_dist.data.cpu().numpy())
        assert len(labels) == len(nns)
        assert len(labels) == len(dists)
        sys.stdout.write("{}/{} \r".format(step, len(dataloader)))
        sys.stdout.flush()
    if jointly_extract_mentions:
        assert sample_idx == len(new_sample_to_all_context_inputs)
        assert ctxt_idx == len(new_samples)
    return labels, nns, dists, cand_dists, new_samples, new_sample_to_all_context_inputs


def _decode_tokens(tokenizer, token_list):
    decoded_string = tokenizer.decode(token_list)
    if isinstance(decoded_string, list):
        decoded_string = decoded_string[0] if len(decoded_string) > 0 else ""
    if "[CLS]" in decoded_string:
        decoded_string = decoded_string[len('[CLS] '):].strip()  # disrgard CLS token
    return decoded_string


def _combine_same_inputs_diff_mention_bounds(samples, labels, nns, dists, sample_to_all_context_inputs, filtered_indices=None, debug=False):
    # TODO save ALL samples
    if not debug:
        try:
            assert len(nns) == sample_to_all_context_inputs[-1][-1] + 1
        except:
            # TODO DEBUG
            import pdb
            pdb.set_trace()
    samples_merged = []
    nns_merged = []
    dists_merged = []
    labels_merged = []
    entity_mention_bounds_idx = []
    filtered_cluster_indices = []  # indices of entire chunks that are filtered out
    dists_idx = 0  # dists is already filtered, use separate idx to keep track of where we are
    for i, context_input_idxs in enumerate(sample_to_all_context_inputs):
        if debug:
            if context_input_idxs[0] >= len(nns):
                break
            elif context_input_idxs[-1] >= len(nns):
                context_input_idxs = context_input_idxs[:context_input_idxs.index(len(nns))]
        # if len(context_input_idxs) == 0:
        #     # should not happen anymore...
        #     import pdb
        #     pdb.set_trace()
        # first filter all filetered_indices
        if filtered_indices is not None:
            context_input_idxs = [idx for idx in context_input_idxs if idx not in filtered_indices]
            # context_input_idxs_filt = []
            # for idx in context_input_idxs:
            #     if idx in filtered_indices:
            #         import pdb
            #         pdb.set_trace()
            #         num_filtered_so_far += 1
            #     else:
            #         context_input_idxs_filt.append(idx - num_filtered_so_far)
            # context_input_idxs = context_input_idxs_filt
        if len(context_input_idxs) == 0:
            filtered_cluster_indices.append(i)
            nns_merged.append(np.array([]))
            dists_merged.append(np.array([]))
            entity_mention_bounds_idx.append(np.array([]))
            labels_merged.append(np.array([]))
            # BOOKMARK
            samples_merged.append({})
            continue
        elif len(context_input_idxs) == 1:  # only 1 example
            nns_merged.append(nns[context_input_idxs[0]])
            # already sorted, don't need to sort more
            dists_merged.append(dists[dists_idx])
            entity_mention_bounds_idx.append(np.zeros(dists[dists_idx].shape, dtype=int))
        else:  # merge refering to same example
            all_distances = np.concatenate([dists[dists_idx + j] for j in range(len(context_input_idxs))], axis=-1)
            all_cand_outputs = np.concatenate([nns[context_input_idxs[j]] for j in range(len(context_input_idxs))], axis=-1)
            dist_sort_idx = np.argsort(-all_distances)  # get in descending order
            nns_merged.append(all_cand_outputs[dist_sort_idx])
            dists_merged.append(all_distances[dist_sort_idx])

            # selected_mention_idx
            # [0,len(dists[0])-1], [len(dists[0]),2*len(dists[0])-1], etc. same range all refer to same mention
            # idx of mention bounds corresponding to entity at nns[example][i]
            entity_mention_bounds_idx.append((dist_sort_idx / len(dists[0])).astype(int))

        for i in range(len(context_input_idxs)):
            assert labels[context_input_idxs[0]] == labels[context_input_idxs[i]]
            assert samples[context_input_idxs[0]]["q_id"] == samples[context_input_idxs[i]]["q_id"]
            assert samples[context_input_idxs[0]]["label"] == samples[context_input_idxs[i]]["label"]
            assert samples[context_input_idxs[0]]["label_id"] == samples[context_input_idxs[i]]["label_id"]
            if "gold_context_left" in samples[context_input_idxs[0]]:
                assert samples[context_input_idxs[0]]["gold_context_left"] == samples[context_input_idxs[i]]["gold_context_left"]
                assert samples[context_input_idxs[0]]["gold_mention"] == samples[context_input_idxs[i]]["gold_mention"]
                assert samples[context_input_idxs[0]]["gold_context_right"] == samples[context_input_idxs[i]]["gold_context_right"]
            if "all_gold_entities" in samples[context_input_idxs[0]]:
                assert samples[context_input_idxs[0]]["all_gold_entities"] == samples[context_input_idxs[i]]["all_gold_entities"]
        
        labels_merged.append(labels[context_input_idxs[0]])
        samples_merged.append({
            "q_id": samples[context_input_idxs[0]]["q_id"],
            "label": samples[context_input_idxs[0]]["label"],
            "label_id": samples[context_input_idxs[0]]["label_id"],
            "context_left": [samples[context_input_idxs[j]]["context_left"] for j in range(len(context_input_idxs))],
            "mention": [samples[context_input_idxs[j]]["mention"] for j in range(len(context_input_idxs))],
            "context_right": [samples[context_input_idxs[j]]["context_right"] for j in range(len(context_input_idxs))],
        })
        if "gold_context_left" in samples[context_input_idxs[0]]:
            samples_merged[len(samples_merged)-1]["gold_context_left"] = samples[context_input_idxs[0]]["gold_context_left"]
            samples_merged[len(samples_merged)-1]["gold_mention"] = samples[context_input_idxs[0]]["gold_mention"]
            samples_merged[len(samples_merged)-1]["gold_context_right"] = samples[context_input_idxs[0]]["gold_context_right"]
        if "all_gold_entities" in samples[context_input_idxs[0]]:
            samples_merged[len(samples_merged)-1]["all_gold_entities"] = samples[context_input_idxs[0]]["all_gold_entities"]
        if "all_gold_entities_pos" in samples[context_input_idxs[0]]:
            samples_merged[len(samples_merged)-1]["all_gold_entities_pos"] = samples[context_input_idxs[0]]["all_gold_entities_pos"]
        dists_idx += len(context_input_idxs)
    return samples_merged, labels_merged, nns_merged, dists_merged, entity_mention_bounds_idx, filtered_cluster_indices


def _retrieve_from_saved_biencoder_outs(save_preds_dir):
    labels = np.load(os.path.join(args.save_preds_dir, "biencoder_labels.npy"))
    nns = np.load(os.path.join(args.save_preds_dir, "biencoder_nns.npy"))
    dists = np.load(os.path.join(args.save_preds_dir, "biencoder_dists.npy"))
    return labels, nns, dists


def load_models(args, logger):
    # load biencoder model
    logger.info("loading biencoder model")
    try:
        with open(args.biencoder_config) as json_file:
            biencoder_params = json.load(json_file)
    except json.decoder.JSONDecodeError:
        with open(args.biencoder_config) as json_file:
            for line in json_file:
                line = line.replace("'", "\"")
                line = line.replace("True", "true")
                line = line.replace("False", "false")
                line = line.replace("None", "null")
                biencoder_params = json.loads(line)
                break
    biencoder_params["path_to_model"] = args.biencoder_model
    biencoder_params["eval_batch_size"] = args.eval_batch_size
    biencoder_params["no_cuda"] = not args.use_cuda
    if biencoder_params["no_cuda"]:
        biencoder_params["data_parallel"] = False
    biencoder_params["load_cand_enc_only"] = False
    # biencoder_params["mention_aggregation_type"] = args.mention_aggregation_type
    biencoder = load_biencoder(biencoder_params)
    if not args.use_cuda and type(biencoder.model).__name__ == 'DataParallel':
        biencoder.model = biencoder.model.module
    elif args.use_cuda and type(biencoder.model).__name__ != 'DataParallel':
        biencoder.model = torch.nn.DataParallel(biencoder.model)

    # load candidate entities
    logger.info("loading candidate entities")

    if args.debug_biencoder:
        candidate_encoding, candidate_token_ids, title2id, id2title, id2text, kb2id, id2kb = _load_candidates(
            "/private/home/belindali/temp/BLINK-Internal/models/entity_debug.jsonl",
            "/private/home/belindali/temp/BLINK-Internal/models/entity_encode_debug.t7",
            "/private/home/belindali/temp/BLINK-Internal/models/entity_ids_debug.t7",  # TODO MAKE THIS FILE!!!!
            biencoder, biencoder_params["max_cand_length"], args.entity_catalogue == args.test_entities, logger=logger
        )
    else:
        (
            candidate_encoding,
            candidate_token_ids,
            title2id,
            id2title,
            id2text,
            wikipedia_id2local_id,
            kb2id,
            id2kb,
        ) = _load_candidates(
            args.entity_catalogue, args.entity_encoding, args.entity_token_ids,
            biencoder, biencoder_params["max_cand_length"], args.entity_catalogue == args.test_entities,
            logger=logger
        )

    return (
        biencoder,
        biencoder_params,
        candidate_encoding,
        candidate_token_ids,
        title2id,
        id2title,
        id2text,
        wikipedia_id2local_id,
        kb2id,
        id2kb,
    )


def run(
    args,
    logger,
    biencoder,
    biencoder_params,
    candidate_encoding,
    candidate_token_ids,
    title2id,
    id2title,
    id2text,
    wikipedia_id2local_id,
    kb2id,
    id2kb,
):

    if not args.test_mentions and not args.interactive and not args.qa_data:
        msg = (
            "ERROR: either you start BLINK with the "
            "interactive option (-i) or you pass in input test mentions (--test_mentions)"
            "and test entities (--test_entities)"
        )
        raise ValueError(msg)
    
    if hasattr(args, 'save_preds_dir') and not os.path.exists(args.save_preds_dir):
        os.makedirs(args.save_preds_dir)
        print("Saving preds in {}".format(args.save_preds_dir))

    print(args)
    print(args.output_path)

    stopping_condition = False
    while not stopping_condition:

        samples = None

        if args.interactive:
            logger.info("interactive mode")

            # biencoder_params["eval_batch_size"] = 1

            # Load NER model
            ner_model = NER.get_model()

            # Interactive
            text = input("insert text:")

            # Identify mentions
            samples = _annotate(ner_model, [text])

            _print_colorful_text(text, samples)
        elif args.qa_data:
            logger.info("QA (WebQSP/GraphQs/NQ) dataset mode")
            # EL for QAdata mode

            #if args.do_ner == "flair":
            #    # Load NER model
            #    ner_model = NER.get_model()

            #    lines = json.load(open(args.test_mentions))
            #    text = [line['utterance'] for line in lines]

            #    # Identify mentions
            #    samples = _annotate(ner_model, text)

            #    kb2id, id2kb = __map_test_entities(args.test_entities, title2id, logger)
            #else:
            logger.info("Loading test samples....")
            samples, kb2id, id2kb, num_unk, sample_to_all_context_inputs = _get_test_samples(
                args.test_mentions, args.test_entities, title2id, kb2id, id2kb, logger,
                qa_data=True, do_ner=args.do_ner, debug=args.debug_biencoder,
                main_entity_only=args.eval_main_entity, do_map_test_entities=(len(kb2id) == 0),
                biencoder=biencoder,
            )
            logger.info("Finished loading test samples")

            if args.debug_biencoder:
                sample_to_all_context_inputs = sample_to_all_context_inputs[:10]
                samples = samples[:sample_to_all_context_inputs[-1][-1] + 1]

            stopping_condition = True
        else:
            logger.info("test dataset mode")

            # Load test mentions
            samples, _, _, _, sample_to_all_context_inputs = _get_test_samples(
                args.test_mentions, args.test_entities, title2id, kb2id, id2kb, logger,
                biencoder=biencoder,
            )
            stopping_condition = True

        # prepare the data for biencoder
        # run biencoder if predictions not saved
        if not os.path.exists(os.path.join(args.save_preds_dir, 'runtime.txt')):
            logger.info("Preparing data for biencoder....")
            dataloader = _process_biencoder_dataloader(
                samples, biencoder.tokenizer, biencoder_params
            )
            logger.info("Finished preparing data for biencoder")

            # run biencoder
            logger.info("Running biencoder...")


            # TODO DELETE THIS
            # cand_encs_npy = np.load("/private/home/belindali/BLINK/models/all_entities_large.npy")  # TODO DONT HARDCODE THESE PATHS
            # d = cand_encs_npy.shape[1]
            # # nsplits = 100
            # cand_encs_flat_index = faiss.IndexFlatIP(d)
            # # cand_encs_quantizer = faiss.IndexFlatIP(d)
            # # assert cand_encs_quantizer.is_trained
            # # cand_encs_index = faiss.IndexIVFFlat(cand_encs_quantizer, d, nsplits, faiss.METRIC_INNER_PRODUCT)
            # # assert not cand_encs_index.is_trained
            # # cand_encs_index.train(cand_encs_npy)  # 15s
            # # assert cand_encs_index.is_trained
            # # cand_encs_index.add(cand_encs_npy)  # 41s
            # cand_encs_flat_index.add(cand_encs_npy)
            # # assert cand_encs_index.ntotal == cand_encs_npy.shape[0]
            # assert cand_encs_flat_index.ntotal == cand_encs_npy.shape[0]

            start_time = time.time()
            labels, nns, dists, cand_dists, new_samples, sample_to_all_context_inputs = _run_biencoder(
                args, biencoder, dataloader, candidate_encoding, samples=samples,
                top_k=args.top_k, device="cpu" if biencoder_params["no_cuda"] else "cuda",
                jointly_extract_mentions=(args.do_ner == "joint"),
                sample_to_all_context_inputs=sample_to_all_context_inputs,
                # num_mentions=int(args.mention_classifier_threshold) if args.do_ner == "joint" else None,
                mention_classifier_threshold=float(args.mention_classifier_threshold) if args.do_ner == "joint" else None,
                # cand_encs_flat_index=cand_encs_flat_index
            )
            end_time = time.time()
            logger.info("Finished running biencoder")

            runtime = end_time - start_time
            
            np.save(os.path.join(args.save_preds_dir, "biencoder_labels.npy"), labels)
            np.save(os.path.join(args.save_preds_dir, "biencoder_nns.npy"), nns)
            np.save(os.path.join(args.save_preds_dir, "biencoder_dists.npy"), dists)
            np.save(os.path.join(args.save_preds_dir, "biencoder_cand_dists.npy"), cand_dists)
            json.dump(new_samples, open(os.path.join(args.save_preds_dir, "samples.json"), "w"))
            json.dump(sample_to_all_context_inputs, open(os.path.join(args.save_preds_dir, "sample_to_all_context_inputs.json"), "w"))
            with open(os.path.join(args.save_preds_dir, "runtime.txt"), "w") as wf:
                wf.write(str(runtime))
        else:
            labels, nns, dists = _retrieve_from_saved_biencoder_outs(args.save_preds_dir)
            runtime = float(open(os.path.join(args.save_preds_dir, "runtime.txt")).read())
            if args.do_ner != "joint" and not os.path.exists(os.path.join(args.save_preds_dir, "samples.json")):
                json.dump(new_samples, open(os.path.join(args.save_preds_dir, "samples.json"), "w"))  # TODO UNCOMMENT
                json.dump(sample_to_all_context_inputs, open(os.path.join(args.save_preds_dir, "sample_to_all_context_inputs.json"), "w"))
            elif args.do_ner == "joint":
                new_samples = json.load(open(os.path.join(args.save_preds_dir, "samples.json")))
                sample_to_all_context_inputs = json.load(open(os.path.join(args.save_preds_dir, "sample_to_all_context_inputs.json")))

        logger.info("Merging inputs...")
        new_samples_merged, labels_merged, nns_merged, dists_merged, entity_mention_bounds_idx, no_pred_indices = _combine_same_inputs_diff_mention_bounds(
            new_samples, labels, nns, dists, sample_to_all_context_inputs,
        )
        logger.info("Finished merging inputs")

        if args.interactive:

            print("\nfast (biencoder) predictions:")

            _print_colorful_text(text, samples)

            # print biencoder prediction
            idx = 0
            for entity_list, sample in zip(nns, samples):
                e_id = entity_list[0]
                e_title = id2title[e_id]
                e_text = id2text[e_id]
                _print_colorful_prediction(idx, sample, e_id, e_title, e_text)
                idx += 1
            print()

            continue

        elif args.qa_data:
            # save biencoder predictions and print precision/recalls
            do_sort = False
            entity_freq_map = {}
            entity_freq_map[""] = 0  # for null cases
            with open("/private/home/belindali/starsem2018-entity-linking/resources/wikidata_entity_freqs.map") as f:
                for line in f:
                    split_line = line.split("\t")
                    entity_freq_map[split_line[0]] = int(split_line[1])
            num_correct = 0
            num_predicted = 0
            num_gold = 0
            save_biencoder_file = os.path.join(args.save_preds_dir, 'biencoder_outs.jsonl')
            all_entity_preds = []
            # check out no_pred_indices
            with open(save_biencoder_file, 'w') as f:
                for i, sample in enumerate(new_samples_merged):
                    if len(sample) == 0:
                        sample = samples[i]
                    entity_list = nns_merged[i]
                    if do_sort:
                        entity_list = entity_list.tolist()
                        entity_list.sort(key=(lambda x: entity_freq_map.get(id2kb.get(id2kb.get(str(x), "")), 0)), reverse=True)
                    e_kbid = None
                    if len(entity_list) > 0:
                        e_id = entity_list[0]
                        e_kbid = id2kb.get(e_id, id2kb.get(str(e_id), ""))
                    pred_kbids_sorted = []
                    for all_id in entity_list:
                        kbid = id2kb.get(all_id, id2kb.get(str(all_id), ""))
                        pred_kbids_sorted.append(kbid)
                    label = labels_merged[i]
                    distances = dists_merged[i]
                    input = ["{}[{}]{}".format(
                        sample['context_left'][j],
                        sample['mention'][j], 
                        sample['context_right'][j],
                    ) for j in range(len(sample['context_left']))]
                    utterance = None
                    if 'gold_context_left' in sample:
                        gold_mention_bounds = "{}[{}]{}".format(sample['gold_context_left'],
                            sample['gold_mention'], sample['gold_context_right'])
                    if 'all_gold_entities_pos' in sample:
                        if isinstance(sample["context_left"], list):
                            utterance = sample["context_left"][0] + sample["mention"][0].strip() + sample["context_right"][0]
                        else:
                            utterance = sample["context_left"] + sample["mention"].strip() + sample["context_right"]
                        gold_mention_bounds_list = []
                        for pos in sample['all_gold_entities_pos']:
                            gold_mention_bounds_list.append("{}[{}]{}".format(
                                utterance[:pos[0]], utterance[pos[0]:pos[1]], utterance[pos[1]:],
                            ))
                        gold_mention_bounds = "; ".join(gold_mention_bounds_list)

                    # assert input == first_input
                    # assert label == first_label
                    # f.write(e_kbid + "\t" + str(sample['label']) + "\t" + str(input) + "\n")

                    if args.eval_main_entity:
                        e_mention_bounds = int(entity_mention_bounds_idx[i][0])
                        if e_kbid != "":
                            gold_triple = [(
                                sample['label'],
                                len(sample['gold_context_left']), 
                                len(sample['gold_context_left']) + len(sample['gold_mention']),
                            )]
                            pred_triple = [(
                                e_kbid,
                                len(sample['context_left'][e_mention_bounds]), 
                                len(sample['context_left'][e_mention_bounds]) + len(sample['mention'][e_mention_bounds]),
                            )]
                            if entity_linking_tp_with_overlap(gold_triple, pred_triple):
                                num_correct += 1
                        num_total += 1
                    else:
                        if "all_gold_entities" in sample:
                            '''
                            get top for each mention bound, w/out duplicates
                            # TOP-1
                            all_pred_entities = pred_kbids_sorted[:1]
                            e_mention_bounds = entity_mention_bounds_idx[i][:1].tolist()
                            # '''
                            if args.final_thresholding == "joint_0":
                                # THRESHOLDING
                                assert utterance is not None
                                top_indices = np.where(distances > 0)[0]
                                all_pred_entities = [pred_kbids_sorted[topi] for topi in top_indices]
                                # already sorted by score
                                e_mention_bounds = [entity_mention_bounds_idx[i][topi] for topi in top_indices]
                            elif args.final_thresholding == "top_joint_by_mention" or args.final_thresholding == "top_entity_by_mention":
                                if len(entity_mention_bounds_idx[i]) == 0:
                                    e_mention_bounds_idxs = []
                                else:
                                    # 1 PER BOUND
                                    try:
                                        e_mention_bounds_idxs = [np.where(entity_mention_bounds_idx[i] == j)[0][0] for j in range(len(sample['context_left']))]
                                    except:
                                        import pdb
                                        pdb.set_trace()
                                # sort bounds
                                e_mention_bounds_idxs.sort()
                                all_pred_entities = []
                                e_mention_bounds = []
                                for bound_idx in e_mention_bounds_idxs:
                                    if pred_kbids_sorted[bound_idx] not in all_pred_entities:
                                        all_pred_entities.append(pred_kbids_sorted[bound_idx])
                                        e_mention_bounds.append(entity_mention_bounds_idx[i][bound_idx])

                            # prune mention overlaps
                            e_mention_bounds_pruned = []
                            all_pred_entities_pruned = []
                            mention_masked_utterance = np.zeros(len(utterance))
                            # ensure well-formed-ness, prune overlaps
                            # greedily pick highest scoring, then prune all overlapping
                            for idx, mb_idx in enumerate(e_mention_bounds):
                                # remove word-boundary demarcators
                                try:
                                    sample['context_left'][mb_idx] = sample['context_left'][mb_idx].replace("##", "")
                                    sample['mention'][mb_idx] = sample['mention'][mb_idx].strip().replace("##", "")
                                    sample['context_right'][mb_idx] = sample['context_right'][mb_idx].replace("##", "")
                                except:
                                    import pdb
                                    pdb.set_trace()
                                # get mention bounds
                                mention_start = len(sample['context_left'][mb_idx])
                                mention_end = len(sample['context_left'][mb_idx]) + len(sample['mention'][mb_idx])
                                # check if in existing mentions
                                try:
                                    if mention_masked_utterance[mention_start] == 1 or mention_masked_utterance[mention_end - 1] == 1:
                                        continue
                                except:
                                    import pdb
                                    pdb.set_trace()
                                e_mention_bounds_pruned.append(mb_idx)
                                all_pred_entities_pruned.append(all_pred_entities[idx])
                                mention_masked_utterance[mention_start:mention_end] = 1
                            
                            pred_triples = [(
                                # sample['all_gold_entities'][i],
                                all_pred_entities_pruned[j], # TODO REVERT THIS
                                len(sample['context_left'][e_mention_bounds_pruned[j]]), 
                                len(sample['context_left'][e_mention_bounds_pruned[j]]) + len(sample['mention'][e_mention_bounds_pruned[j]]),
                            ) for j in range(len(all_pred_entities_pruned))]
                            gold_triples = [(
                                sample['all_gold_entities'][j],
                                sample['all_gold_entities_pos'][j][0], 
                                sample['all_gold_entities_pos'][j][1],
                            ) for j in range(len(sample['all_gold_entities']))]
                            num_correct += entity_linking_tp_with_overlap(gold_triples, pred_triples)
                            num_predicted += len(all_pred_entities_pruned)
                            num_gold += len(sample["all_gold_entities"])
                        
                    # if sample['label'] is not None:
                    #     num_predicted += 1
                    entity_results = {
                        "q_id": sample["q_id"],
                        "top_KBid": e_kbid,
                        "all_gold_entities": sample.get("all_gold_entities", None),
                        # "id": e_id,
                        # "title": e_title,
                        # "text": e_text,
                        "pred_triples": pred_triples,
                        "gold_triples": gold_triples,
                        "sorted_pred_KBids": [id2kb.get(e_id, "") for e_id in entity_list],
                        "input_mention_bounds": input,
                        "gold_mention_bounds": gold_mention_bounds,
                        "gold_KBid": sample['label'],
                        "scores": distances.tolist(),
                    }

                    all_entity_preds.append(entity_results)
                    f.write(
                        json.dumps(entity_results) + "\n"
                    )
            print()
            if num_predicted > 0 and num_gold > 0:
                p = float(num_correct) / float(num_predicted)
                r = float(num_correct) / float(num_gold)
                if p + r > 0:
                    f1 = 2 * p * r / (p + r)
                else:
                    f1 = 0
                print("biencoder precision = {} / {} = {}".format(num_correct, num_predicted, p))
                print("biencoder recall = {} / {} = {}".format(num_correct, num_gold, r))
                print("biencoder f1 = {}".format(f1))
                print("biencoder runtime = {}".format(runtime))

                return f1, r

            if args.do_ner == "none":
                print("number unknown entity examples: {}".format(num_unk))

            # get recall values
            x = []
            y = []
            for i in range(1, args.top_k):
                temp_y = 0.0
                for label, top in zip(labels_merged, nns_merged):
                    if label in top[:i]:
                        temp_y += 1
                if len(labels_merged) > 0:
                    temp_y /= len(labels_merged)
                x.append(i)
                y.append(temp_y)
            # plt.plot(x, y)
            biencoder_accuracy = y[0]
            recall_at = y[-1]
            print("biencoder accuracy: %.4f" % biencoder_accuracy)
            print("biencoder recall@%d: %.4f" % (args.top_k, y[-1]))

            # use only biencoder
            return biencoder_accuracy, recall_at, 0, 0, len(samples)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--debug", "-d", action="store_true", default=False, help="Run debug mode"
    )
    parser.add_argument(
        "--debug_biencoder", "-db", action="store_true", default=False, help="Debug biencoder"
    )
    # evaluation mode
    parser.add_argument(
        "--get_predictions", "-p", action="store_true", default=False, help="Getting predictions mode. Does not filter at crossencoder step."
    )
    parser.add_argument(
        "--eval_main_entity", action="store_true", default=False, help="Main-entity evaluation."
    )
    
    parser.add_argument(
        "--interactive", "-i", action="store_true", help="Interactive mode."
    )

    # test_data
    parser.add_argument(
        "--test_mentions", dest="test_mentions", type=str, help="Test Dataset."
    )
    parser.add_argument(
        "--test_entities", dest="test_entities", type=str, help="Test Entities."
    )

    parser.add_argument(
        "--qa_data", "-q", action="store_true", help="Test Data is QA form"
    )
    parser.add_argument(
        "--save_preds_dir", type=str, help="Directory to save model predictions to."
    )
    parser.add_argument(
        "--do_ner", "-n", type=str, default='none', choices=['joint', 'flair', 'ngram', 'single', 'qa_classifier', 'none'],
        help="Use automatic NER systems. Options: 'joint', 'flair', 'ngram', 'single', 'qa_classifier' 'none'."
        "(Set 'none' to get gold mention bounds from examples)"
    )
    parser.add_argument(
        "--mention_classifier_threshold", type=str, default=None, help="Must be specified if '--do_ner qa_classifier'."
        "Threshold for mention classifier score (either qa or joint) for which examples will be pruned if they fall under that threshold."
    )
    parser.add_argument(
        "--top_k", type=int, default=100, help="Must be specified if '--do_ner qa_classifier'."
        "Number of entity candidates to consider per mention"
    )
    parser.add_argument(
        "--final_thresholding", type=str, default=None, help="How to threshold the final candidates."
        "`top_joint_by_mention`: get top candidate (with joint score) for each predicted mention bound."
        "`top_entity_by_mention`: get top candidate (with entity score) for each predicted mention bound."
        "`joint_0`: by thresholding joint score to > 0."
    )


    # biencoder
    parser.add_argument(
        "--biencoder_model",
        dest="biencoder_model",
        type=str,
        # default="models/biencoder_wiki_large.bin",
        default="models/biencoder_wiki_large.bin",
        help="Path to the biencoder model.",
    )
    parser.add_argument(
        "--biencoder_config",
        dest="biencoder_config",
        type=str,
        # default="models/biencoder_wiki_large.json",
        default="models/biencoder_wiki_large.json",
        help="Path to the biencoder configuration.",
    )
    parser.add_argument(
        "--entity_catalogue",
        dest="entity_catalogue",
        type=str,
        # default="models/tac_entity.jsonl",  # TAC-KBP
        default="models/entity.jsonl",  # ALL WIKIPEDIA!
        help="Path to the entity catalogue.",
    )
    parser.add_argument(
        "--entity_token_ids",
        dest="entity_token_ids",
        type=str,
        default="models/entity_token_ids_128.t7",  # ALL WIKIPEDIA!
        help="Path to the tokenized entity titles + descriptions.",
    )
    parser.add_argument(
        "--entity_encoding",
        dest="entity_encoding",
        type=str,
        # default="models/tac_candidate_encode_large.t7",  # TAC-KBP
        default="models/all_entities_large.t7",  # ALL WIKIPEDIA!
        help="Path to the entity catalogue.",
    )

    parser.add_argument(
        "--eval_batch_size",
        dest="eval_batch_size",
        type=int,
        default=8,
        help="Crossencoder's batch size for evaluation",
    )

    # output folder
    parser.add_argument(
        "--output_path",
        dest="output_path",
        type=str,
        default="output",
        help="Path to the output.",
    )

    parser.add_argument(
        "--fast", dest="fast", action="store_true", help="only biencoder mode"
    )

    parser.add_argument(
        "--use_cuda", dest="use_cuda", action="store_true", default=False, help="run on gpu"
    )

    args = parser.parse_args()

    logger = utils.get_logger(args.output_path)
    logger.setLevel(10)

    models = load_models(args, logger)
    run(args, logger, *models)
