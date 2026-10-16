from benchmarks.load.run import summarize


def test_fast_rejections_do_not_hide_success_latency():
    rows=[{'success':False,'status':503,'error':'http_503','milliseconds':1,'operation':'read'} for _ in range(99)]
    rows.append({'success':True,'status':200,'error':'','milliseconds':2000,'operation':'read'})
    result=summarize(rows,2)
    assert result['success_rate']==.01
    assert result['latency_ms']['p95']==1
    assert result['success_latency_ms']['p95']==2000
    assert result['successful_throughput']==.5
    assert result['throughput']==50
    assert result['statuses']=={'503':99,'200':1}


def test_timeouts_and_no_success_keep_denominators():
    result=summarize([{'success':False,'status':0,'error':'client_timeout','milliseconds':30000,'operation':'search'}],30)
    assert result['success_rate']==0
    assert result['errors']=={'client_timeout':1}
