#!/bin/bash
cd ..
echo 'Running tests...'

# Have to remain disabled for now:
# Failing for unknown reasons: AccessControlTest,StatsStoreDataQueryTest
# Failing for OOM reasons: SqliteDataStoreTest

EXCLUDE_TESTS=AccessControlTest,StatsStoreDataQueryTest,SqliteDataStoreTest,HTTPDataStoreCSVBenchmarks,HTTPDataStoreBenchmarks,MicroBenchmarks,TDBDataStoreBenchmarks,FakeDataStoreBenchmarks,AverageMicroBenchmarks,SqliteDataStoreBenchmarks,DataStoreCSVBenchmarks,CheckLoaderTests,TDBDataStoreCSVBenchmarks,ArtifactKBTest,MultiShardedQueueManagerTest,ConfigActionTest,SearchTest,RegistryVFSTests,OSXDriverTests,RekallTestSuite,GeneralFlowsTest,FlowStateTest,TestCryptoTypeInfos,AFF4Benchmark,GRRArtifactTest,GRRFuseDatastoreOnlyTest,TestOSXFileParsing,AFF4SymlinkTest,FlowTestsBaseclass,ConditionTest,RekallVADParserTest
PYTHONPATH=. python grr/run_tests.py --processes=1 --exclude_tests=$EXCLUDE_TESTS 2>&1|grep -v 'DEBUG:'|grep -v 'INFO:'
TEST_STATUS=${PIPESTATUS[0]}
exit $TEST_STATUS
