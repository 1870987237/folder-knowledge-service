import json
import math

import pytest

from benchmarks.agent.tasks import corpus
from benchmarks.retrieval.run import load_queries, ranking_metrics


def test_known_ranking_and_chunk_deduplication():
    result = ranking_metrics(['x', 'a', 'a', 'b'], ['a', 'b'], 3)
    assert result['recall'] == 1
    assert result['retrieved_documents'] == 3
    assert result['mrr'] == .5
    assert result['precision_at_k'] == 2/3
    assert result['ndcg'] == pytest.approx((1/math.log2(3) + .5)/(1 + 1/math.log2(3)))
    assert ranking_metrics(['a', 'a', 'b'], ['a', 'b'], 2)['recall'] == 1


def test_empty_and_short_rankings_keep_fixed_denominator():
    assert ranking_metrics([], ['a'], 5)['recall'] == 0
    assert ranking_metrics(['a'], ['a', 'b'], 5)['precision_at_k'] == .2
    assert ranking_metrics(['a'], [], 5)['recall'] is None


def test_freeze_does_not_select_successful_or_post_evidence_queries():
    from benchmarks.retrieval.prepare import first_query
    calls=[{'tool':'list_libraries'},{'tool':'search','arguments':{'query':'first'},'is_error':True},
           {'tool':'search','arguments':{'query':'later success'}}]
    assert first_query(calls)=='first'
    with pytest.raises(ValueError):first_query([{'tool':'read_file'},*calls])
    with pytest.raises(ValueError):first_query([{'tool':'search','arguments':{'query':'a'*201}}])


def test_supplied_synthetic_labels_resolve_to_real_documents():
    queries, qrels = load_queries('benchmarks/datasets/basic/retrieval-queries.csv',
        'benchmarks/datasets/basic/retrieval-qrels.json', corpus())
    assert len(queries) == 8 and qrels['Q08'] == []


@pytest.mark.parametrize('query,labels', [('a'*201, ['a.md']), ('short', ['missing.md']), ('short', ['a.md', 'a.md'])])
def test_bad_inputs_fail_before_starting_service(tmp_path, query, labels):
    csv = tmp_path/'queries.csv'; csv.write_text('id,query\nQ1,' + query + '\n', encoding='utf-8')
    qrels = tmp_path/'qrels.json'; qrels.write_text(json.dumps({'Q1': labels}), encoding='utf-8')
    with pytest.raises(ValueError):
        load_queries(csv, qrels, {'a.md': 'synthetic'})
