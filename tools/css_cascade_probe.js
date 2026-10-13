/*
 * CSS 级联审计探针（配合 tools/ui_preview.py --probe @本文件 --probe-out 使用）
 *
 * 解决的问题：截图只能证明"当前可见状态"没变，证明不了 hover / :focus /
 * toast / modal 这些拍不到的状态。而合并"同一选择器被声明多次"这类重构时，
 * 恰恰最容易在这些状态上出错。
 *
 * 输出两段：
 *   第 1 行    重复选择器报告（目标应为 NO_DUPLICATES）
 *   其后       style.css 的「有效声明表」：每个选择器 → 最终生效的属性集合
 *              （按选择器名与属性名排序，因此与声明书写顺序无关）
 *
 * 用法：
 *   # 改动前留基线
 *   py --% -X utf8 tools/ui_preview.py --page param_editor `
 *       --probe @tools/css_cascade_probe.js --probe-out tmp/cascade_before.txt
 *   # 改动后对比（必须逐字节一致）
 *   Compare-Object (Get-Content tmp/cascade_before.txt) (Get-Content tmp/cascade_after.txt)
 */
(function () {
  var sheet = document.styleSheets[1]; // 0=tokens.css, 1=style.css
  if (!sheet) return 'NO_SHEET';

  var map = {};    // 选择器 -> 最终生效的属性集合（同一选择器多条规则时后者胜出）
  var counts = {}; // 选择器 -> 出现次数
  var order = [];

  function walk(rules, prefix) {
    for (var i = 0; i < rules.length; i++) {
      var rule = rules[i];

      if (rule.type === CSSRule.KEYFRAMES_RULE) {
        map[prefix + '@keyframes ' + rule.name] = { cssText: rule.cssText };
        continue;
      }
      if (rule.type === CSSRule.MEDIA_RULE) {
        walk(rule.cssRules, prefix + '@media ' + rule.conditionText + ' ');
        continue;
      }
      if (!rule.selectorText) continue;

      var key = prefix + rule.selectorText;
      if (!counts[key]) { counts[key] = 0; order.push(key); }
      counts[key] += 1;

      if (!map[key]) map[key] = {};
      for (var j = 0; j < rule.style.length; j++) {
        var prop = rule.style[j];
        map[key][prop] = rule.style.getPropertyValue(prop);
      }
    }
  }

  walk(sheet.cssRules, '');

  var dupes = order.filter(function (k) { return counts[k] > 1; });
  var header = dupes.length
    ? 'DUPLICATES(' + dupes.length + '): ' + dupes.map(function (k) {
        return k + ' x' + counts[k];
      }).join(' | ')
    : 'NO_DUPLICATES (' + order.length + ' unique selectors)';

  var table = Object.keys(map).sort().map(function (key) {
    var props = Object.keys(map[key]).sort().map(function (prop) {
      return prop + ':' + map[key][prop];
    });
    return key + ' {' + props.join(';') + '}';
  }).join('\n');

  return header + '\n' + table;
})()
