const test = require('node:test');
const assert = require('node:assert/strict');
const {stats, symlog, windowSamples} = require('../src/fluke8845a/web/iv_math.js');
test('window statistics use valid raw samples and population variance', () => {
  assert.deepEqual(stats([1, 2, 3]), {mean: 2, variance: 2/3, std: Math.sqrt(2/3), median: 2});
  assert.equal(stats([]), null);
});
test('window excludes old and invalid samples', () => {
  assert.deepEqual(windowSamples([{t:0,value:1},{t:3,value:null},{t:4,value:2},{t:6,value:3}], 6, 3), [{t:4,value:2},{t:6,value:3}]);
});
test('signed log preserves zero and direction', () => {
  assert.equal(symlog(0, 1), 0);
  assert.equal(symlog(-10, 1), -symlog(10, 1));
});
