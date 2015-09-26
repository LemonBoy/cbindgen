#!/usr/bin/env python2
import sys, os, StringIO, clang.cindex

type_conv = {
        clang.cindex.TypeKind.VOID:            'void',
        clang.cindex.TypeKind.LONG:            'long',
        clang.cindex.TypeKind.ULONG:           'unsigned-long',
        clang.cindex.TypeKind.LONGLONG:        'integer64',
        clang.cindex.TypeKind.ULONGLONG:       'unsigned-integer64',
        clang.cindex.TypeKind.INT:             'int',
        clang.cindex.TypeKind.UINT:            'unsigned-integer',
        clang.cindex.TypeKind.CHAR_S:          'char',
        clang.cindex.TypeKind.UCHAR:           'byte',
        clang.cindex.TypeKind.SHORT:           'short',
        clang.cindex.TypeKind.USHORT:          'unsigned-short',
        clang.cindex.TypeKind.FLOAT:           'float',
        clang.cindex.TypeKind.DOUBLE:          'double',

        clang.cindex.TypeKind.CONSTANTARRAY:   'c-pointer',
        clang.cindex.TypeKind.INCOMPLETEARRAY: 'c-pointer',
        }

def lispize_name (name):
    # Make 'name' resemble a lisp-ish name
    return name.lower().replace('_', '-')

def resolve_type (t):
    # XXX : Might want to special case the {u,}int{8,16,32,64}_t
    return t.get_canonical()

def is_array (t):
    return t.kind is clang.cindex.TypeKind.CONSTANTARRAY or \
            t.kind is clang.cindex.TypeKind.INCOMPLETEARRAY

def is_record (t):
    return t.kind is clang.cindex.TypeKind.RECORD

def can_translate (t):
    is_pointer = t.kind is clang.cindex.TypeKind.POINTER
    is_enum = t.kind is clang.cindex.TypeKind.ENUM

    return is_pointer or is_enum or t.kind in type_conv

def translate_type (t):
    # Translate the given type into a FFI-compatible one
    is_pointer = t.kind is clang.cindex.TypeKind.POINTER
    is_enum = t.kind is clang.cindex.TypeKind.ENUM

    if is_pointer:
        pointee_type = resolve_type(t.get_pointee ())
        is_const = pointee_type.is_const_qualified()

        # Let's try to be smart and detect whether this is a c-string
        if is_const and pointee_type.kind is clang.cindex.TypeKind.CHAR_S:
            return 'c-string'

        # Show the struct/union type name to be more informative
        if pointee_type.kind is clang.cindex.TypeKind.RECORD:
            return '(c-pointer "{0}")'.format(pointee_type.spelling)
        # If we can map the pointee type to a ffi type add the type annotation
        elif can_translate(pointee_type):
            return '(c-pointer {0})'.format(translate_type(pointee_type))
        else:
            return 'c-pointer'

    elif is_enum:
        if t.spelling.startswith('enum '):
            return '(enum "{0}")'.format(t.spelling[5:])
        else:
            return 'int'

    if t.kind in type_conv:
        return type_conv[t.kind]

    # Can't reach this
    raise Exception('Unknown type {0}'.format(t.kind.spelling))

def parse_fun (w, fun):
    assert(fun.kind is clang.cindex.CursorKind.FUNCTION_DECL)

    if fun.type.is_function_variadic ():
        print('Cannot translate the function {0}: Variadic function'.format(fun.spelling))
        return

    # Thaw the iterator so that we can use len() on it
    args = [resolve_type(x.type) for x in fun.get_arguments ()]
    ret_type = resolve_type(fun.result_type)

    # Check whether we can resolve the argument types and the return one.
    # This returns False if the function passes structs by value
    v = can_translate(ret_type) and all(can_translate(x) for x in args)
    if v == False:
        # print([x.kind.spelling for x in args])
        print('Cannot translate the function {0}: Type error'.format(fun.spelling))
        return

    # Translate the arguments
    prototype = map(translate_type, args)

    w.write('(define {0}\n  (foreign-lambda {1} {2} {3}))\n'.format(
        lispize_name(fun.spelling), # Lisp-y unction name
        translate_type(ret_type),   # Return type
        fun.spelling,               # Original function name
        ' '.join(prototype)))       # Parameters

def parse_record (w, rec):
    assert(rec.kind is clang.cindex.CursorKind.STRUCT_DECL)

    base_name = lispize_name(rec.spelling)
    is_opaque = rec.type.get_size() < 2

    if is_opaque or base_name == "":
        return

    # If the structure contains a structure definition or has an array (of
    # constant size or an empty one) we mark it as complex
    def discriminate (x):
        return x in [clang.cindex.TypeKind.CONSTANTARRAY,
                clang.cindex.TypeKind.INCOMPLETEARRAY,
                clang.cindex.TypeKind.RECORD]
    fields = {x.spelling: resolve_type(x.type) for x in rec.type.get_fields()}
    is_complex = any(discriminate(v.kind) for (_, v) in fields.items()) 

    # Give up
    if is_complex:
        return

    args = ''
    for (k,v) in fields.items():
        args += '  ({0} {1}-{2} {1}-{2}-set!)\n'.format(
                translate_type(v),
                base_name,
                lispize_name(k))

    w.write('(define-foreign-record-type ({0} "{1}")\n{2})\n'.format(
        base_name,
        rec.spelling,
        args))

def parse_enum (w, enu):
    assert(enu.kind is clang.cindex.CursorKind.ENUM_DECL)

    items = [item.spelling for item in enu.get_children ()]
    prefix = os.path.commonprefix(items)

    if enu.spelling == '':
        # Anonymous enum
        # Use the longest common prefix as name
        if prefix == '':
            return

        base_name = prefix.rstrip(' _')
    else:
        base_name = enu.spelling

    base_name = lispize_name(base_name)

    # Make the enum names even nicer by stripping the common prefix
    items_name = [x[len(prefix):] for x in items]

    # What type the enum maps to ?
    # XXX : Currently not used, we assume it's always an int
    enum_type = resolve_type(enu.enum_type)

    w.write('(define-foreign-enum-type ({0} int)\n  ({0}->int int->{0})\n{1})\n'.format(
        base_name, # Enum name
        '\n'.join(['  (({0}) {1})'.format(lispize_name(y), x) for (x,y) in zip(items, items_name)])))

def node_is_fun (x):
    return x.kind is clang.cindex.CursorKind.FUNCTION_DECL and \
            x.type.kind is clang.cindex.TypeKind.FUNCTIONPROTO
def node_is_enum (x):
    return x.kind is clang.cindex.CursorKind.ENUM_DECL
def node_is_record (x):
    return x.kind is clang.cindex.CursorKind.STRUCT_DECL

def do (path):
    index = clang.cindex.Index.create()
    tu = index.parse(path)

    out = StringIO.StringIO()

    nodes = list(tu.cursor.get_children())

    record_decls = filter(node_is_record, nodes)
    fun_decls = filter(node_is_fun, nodes)
    enum_decls = filter(node_is_enum, nodes)

    # for r in record_decls:
    #     parse_record(out, r)

    for f in fun_decls:
        parse_fun(out, f)

    for e in enum_decls:
        parse_enum(out, e)

    print(out.getvalue())

if __name__ == '__main__':
    for arg in sys.argv[1:]: do(arg)
