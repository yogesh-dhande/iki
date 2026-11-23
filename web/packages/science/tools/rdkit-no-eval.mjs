// RDKit.js's loader, made to run without evaluating strings as code, so Iki's component
// frames, whose policy allows compiling WebAssembly but not `eval` or `new Function`
// (no 'unsafe-eval', ADR 0084), can run it.
//
// RDKit.js is built with Emscripten's embind, which makes the JavaScript side of each
// bound C++ function by writing its source and compiling it with `new Function`, in two
// places: the invoker of each function JavaScript calls (`createJsInvoker`), and of each
// JavaScript method C++ calls (`_emval_create_invoker`). Emscripten avoids them itself
// in builds without dynamic code (emval takes a closure under `-sDYNAMIC_EXECUTION=0`,
// and newer releases make embind's invokers ahead of time), and so does this: it swaps
// each for a closure doing exactly what the source it wrote did, and nothing else in
// RDKit.js changes.
//
// Each is found by its exact text, so a release of RDKit.js that changes them fails the
// build, to be looked at again, rather than slipping through.

/** embind's createJsInvoker, as RDKit.js 2026.9.2 has it. */
const INVOKER =
  'function createJsInvoker(argTypes,isClassMethodFunc,returns,isAsync){var needsDestructorStack=usesDestructorStack(argTypes);var argsNeedStack=argsUseStackAlloc(argTypes);var useStackFrame=argsNeedStack&&!isAsync&&!needsDestructorStack;if(argsNeedStack&&!useStackFrame)needsDestructorStack=true;var argCount=argTypes.length-2;var argsList=[];var argsListWired=["fn"];if(isClassMethodFunc)argsListWired.push("thisWired");for(var i=0;i<argCount;++i){argsList.push(`arg${i}`);argsListWired.push(`arg${i}Wired`)}argsList=argsList.join();argsListWired=argsListWired.join();var invokerFnBody=`return function (${argsList}) {\\n`;if(needsDestructorStack)invokerFnBody+="var destructors = [];\\n";if(useStackFrame)invokerFnBody+="var sp = stackSave();\\ntry {\\n";var dtorStack=needsDestructorStack?"destructors":"null";var args1=["humanName","throwBindingError","invoker","fn","runDestructors","fromRetWire","toClassParamWire"];if(useStackFrame)args1.push("stackSave","stackRestore");if(isClassMethodFunc)invokerFnBody+=`var thisWired = toClassParamWire(${dtorStack}, this);\\n`;for(i=0;i<argCount;++i){var argName=`toArg${i}Wire`;invokerFnBody+=`var arg${i}Wired = ${argName}(${dtorStack}, arg${i});\\n`;args1.push(argName)}invokerFnBody+=(returns||isAsync?"var rv = ":"")+`invoker(${argsListWired});\\n`;if(useStackFrame)invokerFnBody+="} finally {\\nstackRestore(sp);\\n}\\n";if(needsDestructorStack)invokerFnBody+="runDestructors(destructors);\\n";else for(i=isClassMethodFunc?1:2;i<argTypes.length;++i){var paramName=i===1?"thisWired":`arg${i-2}Wired`;if(argTypes[i].destructorFunction!==null){invokerFnBody+=`${paramName}_dtor(${paramName});\\n`;args1.push(`${paramName}_dtor`)}}if(returns)invokerFnBody+="var ret = fromRetWire(rv);\\nreturn ret;\\n";invokerFnBody+="}\\n";return new Function(args1,invokerFnBody)}';

/** The same, as a closure: given what craftInvokerFunction gives the function that
 * source compiles to (in its order), the invoker that source is. */
const INVOKER_CLOSURE = `function createJsInvoker(argTypes,isClassMethodFunc,returns,isAsync){
  var needsDestructorStack=usesDestructorStack(argTypes);
  var argsNeedStack=argsUseStackAlloc(argTypes);
  var useStackFrame=argsNeedStack&&!isAsync&&!needsDestructorStack;
  if(argsNeedStack&&!useStackFrame)needsDestructorStack=true;
  var argCount=argTypes.length-2;
  // The parameters with destructors of their own: 1 is \`this\`, 2 on the arguments.
  var withDestructors=[];
  if(!needsDestructorStack)for(var i=isClassMethodFunc?1:2;i<argTypes.length;++i)if(argTypes[i].destructorFunction!==null)withDestructors.push(i);
  return function(humanName,throwBindingError,invoker,fn,runDestructors,fromRetWire,toClassParamWire,...rest){
    var saveStack=useStackFrame?rest[0]:null;
    var restoreStack=useStackFrame?rest[1]:null;
    var first=useStackFrame?2:0;
    var toArgWire=rest.slice(first,first+argCount);
    var parameterDestructors=rest.slice(first+argCount);
    return function(...args){
      var destructors=needsDestructorStack?[]:null;
      var thisWired;
      var wired=new Array(argCount);
      var rv;
      var sp=useStackFrame?saveStack():0;
      try{
        if(isClassMethodFunc)thisWired=toClassParamWire(destructors,this);
        for(var i=0;i<argCount;++i)wired[i]=toArgWire[i](destructors,args[i]);
        rv=isClassMethodFunc?invoker(fn,thisWired,...wired):invoker(fn,...wired);
      }finally{
        if(useStackFrame)restoreStack(sp);
      }
      if(needsDestructorStack)runDestructors(destructors);
      else for(var k=0;k<withDestructors.length;++k){
        var at=withDestructors[k];
        parameterDestructors[k](at===1?thisWired:wired[at-2]);
      }
      if(returns)return fromRetWire(rv);
    };
  };
}`;

/** Where _emval_create_invoker compiles the source it wrote. */
const EMVAL =
  "functionBody=`return function (handle, methodName, destructorsRef, args) {\\n${functionBody}\\n}`;var invokerFunction=new Function(Object.keys(captures),functionBody)(...Object.values(captures));";

/** The function that source is, as a closure: kind 0 calls the value, 1 calls its
 * method, 2 constructs it, 3 gives the (last) argument; each argument read from `args`,
 * 8 bytes apart. */
const EMVAL_CLOSURE = `var invokerFunction=function(handle,methodName,destructorsRef,args){
  var values=argFromPtr.map((read,i)=>read(args+i*8));
  var rv;
  switch(kind){
    case 0:rv=Emval.toValue(handle)(...values);break;
    case 2:rv=new(Emval.toValue(handle))(...values);break;
    case 3:rv=values[values.length-1];break;
    case 1:rv=Emval.toValue(handle)[getStringOrSymbol(methodName)](...values);break;
  }
  if(!retType.isVoid)return emval_returnValue(toReturnWire,destructorsRef,rv);
};`;

function swap(source, from, to, what) {
  const at = source.indexOf(from);
  if (at === -1 || source.indexOf(from, at + 1) !== -1) {
    throw new Error(
      `RDKit.js's ${what} isn't as this build knows it: look at it again (tools/rdkit-no-eval.mjs).`,
    );
  }
  return source.slice(0, at) + to + source.slice(at + from.length);
}

/** RDKit.js's loader, evaluating no strings as code. */
export function withoutEval(loader) {
  let patched = swap(loader, INVOKER, INVOKER_CLOSURE, "createJsInvoker");
  patched = swap(patched, EMVAL, EMVAL_CLOSURE, "_emval_create_invoker");
  for (const dynamic of [/\bnew Function\b/, /[^\w.$]Function\(/, /\beval\(/]) {
    if (dynamic.test(patched)) {
      throw new Error(`RDKit.js's loader still evaluates code (${dynamic}): look at it again.`);
    }
  }
  return patched;
}
