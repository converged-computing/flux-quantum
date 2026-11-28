#!/bin/bash
for r in us-west-1 us-east-1 eu-north-1; do
  for a in $(aws braket search-jobs --region $r --filters \
      '[{"name":"jobName","operator":"CONTAINS","values":["flux-quantum-hold"]}]' \
      --query 'jobs[?status==`QUEUED`||status==`RUNNING`].jobArn' --output text); do
    echo "cancelling $a"
    aws braket cancel-job --region $r --job-arn "$a"
  done
done
