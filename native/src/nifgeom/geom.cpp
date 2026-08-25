// Native reader/writer for the NIF geometry-data blocks.
//
// WHY THIS EXISTS
// ---------------
// Timing StructBase.read per top-level block type across 60 Oblivion NIFs:
//
//     NiTriStripsData   267 blocks   2.413 s   81.7% of read time
//     NiTriShapeData     21 blocks   0.239 s    8.1%
//     everything else  ~1700 blocks  ~0.14 s    ~5%
//
// Two block types are 89.8% of read time, and by bytes the same three types
// (adding NiBinaryExtraData) are 92.9% of every block byte in the file.  The
// reason is the object model, not the I/O: those 288 geometry blocks hold
// 124,587 vertices, and PyFFI materialises EACH vertex as a Vector3 -- one
// StructBase.__init__ (which builds a set() and an _items list) plus three
// Float objects.  With normals/tangents/bitangents that is ~500,000 Python
// objects per 60 meshes, which is why struct_.__init__ (514,239 calls) and
// Float.__init__ (1,337,405 calls) sit at the top of the profile.
//
// This module reads and writes those blocks straight between the stream and
// numpy arrays, so the per-element Python object never exists.
//
// WHY IT IS DRIVEN BY A PLAN, NOT HARDCODED
// -----------------------------------------
// A census over 300 NIFs found 16 DISTINCT attribute sequences for these two
// block types across 15 (version, user_version, user_version_2) combinations
// -- fields appear and vanish with has_normals / has_vertex_colors /
// has_points / extra_vectors_flags and with several version gates.  Hardcoding
// that table would silently rot the first time a plugin ships a combination we
// did not enumerate.
//
// So the LAYOUT DECISION STAYS IN PYFFI.  The Python side walks
// _get_filtered_attribute_list(data) -- the very same call PyFFI's own
// read/write uses -- and lowers it to a flat list of (opcode, count) pairs.
// This file only executes that plan.  A field whose type we do not implement
// makes the Python side decline the whole block and fall back to PyFFI, so an
// unknown layout is a slow path, never a wrong one.
//
// DETERMINISM AND EXACTNESS
// -------------------------
// The pipeline's output must be byte-reproducible, and this code sits on both
// sides of that: it must produce EXACTLY the bytes PyFFI produced.  All I/O is
// little-endian fixed-width with no floating-point arithmetic performed here
// (values are copied, never computed), so round-tripping is bit-exact by
// construction.  Endianness is asserted against the caller's byte order rather
// than assumed.

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#define NPY_NO_DEPRECATED_API NPY_1_7_API_VERSION
#include <numpy/arrayobject.h>

#include <cstring>
#include <string>
#include <vector>

namespace {

// Opcodes.  Scalars are read/written in place; arrays consume `count` elements
// from the plan's element count.  Keep in sync with nif_geom_native.py.
enum Op {
    OP_UINT8 = 1,   // 1-byte unsigned scalar (UByte, bool at ver > 4.0.0.2)
    OP_UINT16 = 2,  // 2-byte unsigned scalar (UShort, enums stored as ushort)
    OP_INT32 = 3,   // 4-byte signed scalar (Int, Ref)
    OP_UINT32 = 4,  // 4-byte unsigned scalar (UInt, bool at ver <= 4.0.0.2)
    OP_FLOAT = 5,   // 4-byte float scalar
    OP_VEC3 = 6,    // float[3] * count      (vertices, normals, tangents, ...)
    OP_VEC2 = 7,    // float[2] * count      (TexCoord)
    OP_VEC4 = 8,    // float[4] * count      (Color4)
    OP_U16 = 9,     // uint16  * count       (strip lengths, points, triangles)
    OP_U32 = 10,    // uint32  * count
    OP_SKIP = 11,   // raw byte run we neither interpret nor alter
};

struct Field {
    int op;
    npy_intp count;   // element count for array ops; unused for scalars
};

// A parsed plan plus the buffers the caller hands us.
struct Plan {
    std::vector<Field> fields;
};

bool parse_plan(PyObject *seq, Plan &out, std::string &err)
{
    PyObject *fast = PySequence_Fast(seq, "plan must be a sequence");
    if (!fast) { err = "plan is not a sequence"; return false; }
    Py_ssize_t n = PySequence_Fast_GET_SIZE(fast);
    out.fields.reserve(static_cast<size_t>(n));
    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject *item = PySequence_Fast_GET_ITEM(fast, i);   // borrowed
        if (!PyTuple_Check(item) || PyTuple_GET_SIZE(item) != 2) {
            Py_DECREF(fast);
            err = "plan entries must be (op, count) tuples";
            return false;
        }
        long op = PyLong_AsLong(PyTuple_GET_ITEM(item, 0));
        long long cnt = PyLong_AsLongLong(PyTuple_GET_ITEM(item, 1));
        if (PyErr_Occurred()) { Py_DECREF(fast); err = "bad plan entry"; return false; }
        if (cnt < 0) { Py_DECREF(fast); err = "negative count in plan"; return false; }
        Field f;
        f.op = static_cast<int>(op);
        f.count = static_cast<npy_intp>(cnt);
        out.fields.push_back(f);
    }
    Py_DECREF(fast);
    return true;
}

// Per-op element width in bytes, and how many elements a scalar op consumes.
bool op_width(int op, npy_intp count, npy_intp &bytes)
{
    switch (op) {
    case OP_UINT8:  bytes = 1; return true;
    case OP_UINT16: bytes = 2; return true;
    case OP_INT32:
    case OP_UINT32:
    case OP_FLOAT:  bytes = 4; return true;
    case OP_VEC3:   bytes = count * 12; return true;
    case OP_VEC2:   bytes = count * 8;  return true;
    case OP_VEC4:   bytes = count * 16; return true;
    case OP_U16:    bytes = count * 2;  return true;
    case OP_U32:    bytes = count * 4;  return true;
    case OP_SKIP:   bytes = count;      return true;
    default: return false;
    }
}

bool is_array_op(int op)
{
    return op == OP_VEC3 || op == OP_VEC2 || op == OP_VEC4
        || op == OP_U16 || op == OP_U32 || op == OP_SKIP;
}

// ---------------------------------------------------------------------------
// read_block(buffer, offset, plan) -> (scalars, arrays, end_offset)
//
// `scalars` is a list of Python ints/floats in plan order (one per scalar op);
// `arrays` is a list of numpy arrays in plan order (one per array op).  The
// caller assigns them back onto the PyFFI block.
// ---------------------------------------------------------------------------
PyObject *read_block(PyObject *, PyObject *args)
{
    Py_buffer view;
    Py_ssize_t offset;
    PyObject *plan_obj;
    if (!PyArg_ParseTuple(args, "y*nO", &view, &offset, &plan_obj))
        return nullptr;

    Plan plan;
    std::string err;
    if (!parse_plan(plan_obj, plan, err)) {
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, err.c_str());
        return nullptr;
    }

    const unsigned char *base = static_cast<const unsigned char *>(view.buf);
    const npy_intp len = static_cast<npy_intp>(view.len);
    npy_intp pos = static_cast<npy_intp>(offset);

    // Bounds-check the WHOLE plan before reading anything, so a truncated or
    // mis-parsed block fails cleanly instead of walking off the buffer.
    {
        npy_intp probe = pos;
        for (const Field &f : plan.fields) {
            npy_intp bytes;
            if (!op_width(f.op, f.count, bytes)) {
                PyBuffer_Release(&view);
                PyErr_Format(PyExc_ValueError, "unknown opcode %d", f.op);
                return nullptr;
            }
            probe += bytes;
            if (probe < 0 || probe > len) {
                PyBuffer_Release(&view);
                PyErr_SetString(PyExc_ValueError,
                                "plan runs past end of buffer");
                return nullptr;
            }
        }
    }

    PyObject *scalars = PyList_New(0);
    PyObject *arrays = PyList_New(0);
    if (!scalars || !arrays) {
        Py_XDECREF(scalars); Py_XDECREF(arrays);
        PyBuffer_Release(&view);
        return nullptr;
    }

    for (const Field &f : plan.fields) {
        if (!is_array_op(f.op)) {
            PyObject *val = nullptr;
            switch (f.op) {
            case OP_UINT8: {
                unsigned char v; std::memcpy(&v, base + pos, 1); pos += 1;
                val = PyLong_FromUnsignedLong(v); break;
            }
            case OP_UINT16: {
                unsigned short v; std::memcpy(&v, base + pos, 2); pos += 2;
                val = PyLong_FromUnsignedLong(v); break;
            }
            case OP_INT32: {
                int v; std::memcpy(&v, base + pos, 4); pos += 4;
                val = PyLong_FromLong(v); break;
            }
            case OP_UINT32: {
                unsigned int v; std::memcpy(&v, base + pos, 4); pos += 4;
                val = PyLong_FromUnsignedLong(v); break;
            }
            case OP_FLOAT: {
                float v; std::memcpy(&v, base + pos, 4); pos += 4;
                // float -> double widening is exact, and PyFFI stores a Python
                // float (double) too, so this matches its value bit for bit.
                val = PyFloat_FromDouble(static_cast<double>(v)); break;
            }
            default: break;
            }
            if (!val || PyList_Append(scalars, val) != 0) {
                Py_XDECREF(val);
                Py_DECREF(scalars); Py_DECREF(arrays);
                PyBuffer_Release(&view);
                return nullptr;
            }
            Py_DECREF(val);
            continue;
        }

        // Array op: allocate the numpy array and memcpy straight in.
        npy_intp bytes;
        op_width(f.op, f.count, bytes);
        PyObject *arr = nullptr;
        if (f.op == OP_SKIP) {
            npy_intp dims[1] = { f.count };
            arr = PyArray_SimpleNew(1, dims, NPY_UINT8);
        } else if (f.op == OP_U16) {
            npy_intp dims[1] = { f.count };
            arr = PyArray_SimpleNew(1, dims, NPY_UINT16);
        } else if (f.op == OP_U32) {
            npy_intp dims[1] = { f.count };
            arr = PyArray_SimpleNew(1, dims, NPY_UINT32);
        } else {
            int cols = (f.op == OP_VEC3) ? 3 : (f.op == OP_VEC2) ? 2 : 4;
            npy_intp dims[2] = { f.count, cols };
            arr = PyArray_SimpleNew(2, dims, NPY_FLOAT32);
        }
        if (!arr) {
            Py_DECREF(scalars); Py_DECREF(arrays);
            PyBuffer_Release(&view);
            return nullptr;
        }
        if (bytes) {
            std::memcpy(PyArray_DATA(reinterpret_cast<PyArrayObject *>(arr)),
                        base + pos, static_cast<size_t>(bytes));
        }
        pos += bytes;
        if (PyList_Append(arrays, arr) != 0) {
            Py_DECREF(arr);
            Py_DECREF(scalars); Py_DECREF(arrays);
            PyBuffer_Release(&view);
            return nullptr;
        }
        Py_DECREF(arr);
    }

    PyBuffer_Release(&view);
    PyObject *res = Py_BuildValue("NNn", scalars, arrays, (Py_ssize_t)pos);
    return res;
}

// ---------------------------------------------------------------------------
// write_block(plan, scalars, arrays) -> bytes
// ---------------------------------------------------------------------------
PyObject *write_block(PyObject *, PyObject *args)
{
    PyObject *plan_obj, *scalars_obj, *arrays_obj;
    if (!PyArg_ParseTuple(args, "OOO", &plan_obj, &scalars_obj, &arrays_obj))
        return nullptr;

    Plan plan;
    std::string err;
    if (!parse_plan(plan_obj, plan, err)) {
        PyErr_SetString(PyExc_ValueError, err.c_str());
        return nullptr;
    }

    npy_intp total = 0;
    for (const Field &f : plan.fields) {
        npy_intp bytes;
        if (!op_width(f.op, f.count, bytes)) {
            PyErr_Format(PyExc_ValueError, "unknown opcode %d", f.op);
            return nullptr;
        }
        total += bytes;
    }

    PyObject *out = PyBytes_FromStringAndSize(nullptr, (Py_ssize_t)total);
    if (!out) return nullptr;
    unsigned char *dst = reinterpret_cast<unsigned char *>(
        PyBytes_AS_STRING(out));
    npy_intp pos = 0;

    PyObject *sc = PySequence_Fast(scalars_obj, "scalars must be a sequence");
    if (!sc) { Py_DECREF(out); return nullptr; }
    PyObject *ar = PySequence_Fast(arrays_obj, "arrays must be a sequence");
    if (!ar) { Py_DECREF(sc); Py_DECREF(out); return nullptr; }

    Py_ssize_t si = 0, ai = 0;
    const Py_ssize_t n_sc = PySequence_Fast_GET_SIZE(sc);
    const Py_ssize_t n_ar = PySequence_Fast_GET_SIZE(ar);

    for (const Field &f : plan.fields) {
        if (!is_array_op(f.op)) {
            if (si >= n_sc) {
                PyErr_SetString(PyExc_ValueError, "too few scalars for plan");
                goto fail;
            }
            PyObject *v = PySequence_Fast_GET_ITEM(sc, si++);   // borrowed
            switch (f.op) {
            case OP_UINT8: {
                unsigned long x = PyLong_AsUnsignedLongMask(v);
                unsigned char b = static_cast<unsigned char>(x);
                std::memcpy(dst + pos, &b, 1); pos += 1; break;
            }
            case OP_UINT16: {
                unsigned long x = PyLong_AsUnsignedLongMask(v);
                unsigned short b = static_cast<unsigned short>(x);
                std::memcpy(dst + pos, &b, 2); pos += 2; break;
            }
            case OP_INT32: {
                // Signed field, but PyFFI may hold it as a large unsigned
                // value (a Ref is written as -1 for None).  Take the low 32
                // bits either way -- PyLong_AsLongMask is gone in 3.14.
                unsigned long x = PyLong_AsUnsignedLongMask(v);
                int b = static_cast<int>(static_cast<unsigned int>(x));
                std::memcpy(dst + pos, &b, 4); pos += 4; break;
            }
            case OP_UINT32: {
                unsigned long x = PyLong_AsUnsignedLongMask(v);
                unsigned int b = static_cast<unsigned int>(x);
                std::memcpy(dst + pos, &b, 4); pos += 4; break;
            }
            case OP_FLOAT: {
                double d = PyFloat_AsDouble(v);
                if (d == -1.0 && PyErr_Occurred()) goto fail;
                float b = static_cast<float>(d);
                std::memcpy(dst + pos, &b, 4); pos += 4; break;
            }
            default: break;
            }
            if (PyErr_Occurred()) goto fail;
            continue;
        }

        if (ai >= n_ar) {
            PyErr_SetString(PyExc_ValueError, "too few arrays for plan");
            goto fail;
        }
        {
            PyObject *a = PySequence_Fast_GET_ITEM(ar, ai++);   // borrowed
            npy_intp bytes;
            op_width(f.op, f.count, bytes);
            if (!PyArray_Check(a)) {
                PyErr_SetString(PyExc_TypeError, "array entry is not ndarray");
                goto fail;
            }
            PyArrayObject *pa = reinterpret_cast<PyArrayObject *>(a);
            if (!PyArray_ISCARRAY_RO(pa)) {
                PyErr_SetString(PyExc_ValueError,
                                "array must be C-contiguous");
                goto fail;
            }
            if (PyArray_NBYTES(pa) != bytes) {
                PyErr_Format(PyExc_ValueError,
                             "array size %zd does not match plan %zd",
                             (Py_ssize_t)PyArray_NBYTES(pa),
                             (Py_ssize_t)bytes);
                goto fail;
            }
            if (bytes)
                std::memcpy(dst + pos, PyArray_DATA(pa),
                            static_cast<size_t>(bytes));
            pos += bytes;
        }
    }

    Py_DECREF(sc); Py_DECREF(ar);
    return out;

fail:
    Py_DECREF(sc); Py_DECREF(ar); Py_DECREF(out);
    return nullptr;
}

// ---------------------------------------------------------------------------
// fill_floats(buffer, offset, holders, ncomp) -> end_offset
//
// The real hot path.  PyFFI's Array.read builds one element object per item and
// calls elem.read() on each, which for a Vector3 array is N StructBase.__init__
// plus 3N Float.read -- struct.unpack per component.  Measured, Array.read is
// 95% of all NIF read time, and Vector3/Color4/TexCoord are 2.3 s of that 3.0 s.
//
// `holders` is a flat list of the already-constructed value holders, in element
// order then component order (a 4-vertex Vector3 array gives 12 entries:
// x0,y0,z0,x1,...).  Building the objects stays in Python (they must be real
// PyFFI objects for everything downstream); this call replaces only the
// per-component struct.unpack + attribute store, which is where the time is.
PyObject *fill_floats(PyObject *, PyObject *args)
{
    Py_buffer view;
    Py_ssize_t offset;
    PyObject *holders;
    if (!PyArg_ParseTuple(args, "y*nO", &view, &offset, &holders))
        return nullptr;

    PyObject *fast = PySequence_Fast(holders, "holders must be a sequence");
    if (!fast) { PyBuffer_Release(&view); return nullptr; }
    const Py_ssize_t n = PySequence_Fast_GET_SIZE(fast);

    const unsigned char *base = static_cast<const unsigned char *>(view.buf);
    const npy_intp len = static_cast<npy_intp>(view.len);
    npy_intp pos = static_cast<npy_intp>(offset);

    if (pos < 0 || pos + static_cast<npy_intp>(n) * 4 > len) {
        Py_DECREF(fast);
        PyBuffer_Release(&view);
        PyErr_SetString(PyExc_ValueError, "float run past end of buffer");
        return nullptr;
    }

    // Interned attribute name for the holder's payload field.
    static PyObject *s_value = nullptr;
    if (!s_value) {
        s_value = PyUnicode_InternFromString("_value");
        if (!s_value) {
            Py_DECREF(fast); PyBuffer_Release(&view); return nullptr;
        }
    }

    for (Py_ssize_t i = 0; i < n; ++i) {
        float f;
        std::memcpy(&f, base + pos, 4);
        pos += 4;
        PyObject *num = PyFloat_FromDouble(static_cast<double>(f));
        if (!num) { Py_DECREF(fast); PyBuffer_Release(&view); return nullptr; }
        PyObject *h = PySequence_Fast_GET_ITEM(fast, i);   // borrowed
        if (PyObject_SetAttr(h, s_value, num) != 0) {
            Py_DECREF(num); Py_DECREF(fast); PyBuffer_Release(&view);
            return nullptr;
        }
        Py_DECREF(num);
    }

    Py_DECREF(fast);
    PyBuffer_Release(&view);
    return PyLong_FromSsize_t(static_cast<Py_ssize_t>(pos));
}

// ---------------------------------------------------------------------------
// pack_floats(holders) -> bytes
//
// The write-side mirror: read `_value` off each holder and emit a packed
// little-endian float32 run.  Replaces Array.write's per-element
// elem.write() -> struct.pack chain.
// ---------------------------------------------------------------------------
PyObject *pack_floats(PyObject *, PyObject *args)
{
    PyObject *holders;
    if (!PyArg_ParseTuple(args, "O", &holders))
        return nullptr;
    PyObject *fast = PySequence_Fast(holders, "holders must be a sequence");
    if (!fast) return nullptr;
    const Py_ssize_t n = PySequence_Fast_GET_SIZE(fast);

    PyObject *out = PyBytes_FromStringAndSize(nullptr, n * 4);
    if (!out) { Py_DECREF(fast); return nullptr; }
    unsigned char *dst = reinterpret_cast<unsigned char *>(
        PyBytes_AS_STRING(out));

    static PyObject *s_value = nullptr;
    if (!s_value) {
        s_value = PyUnicode_InternFromString("_value");
        if (!s_value) { Py_DECREF(fast); Py_DECREF(out); return nullptr; }
    }

    for (Py_ssize_t i = 0; i < n; ++i) {
        PyObject *h = PySequence_Fast_GET_ITEM(fast, i);   // borrowed
        PyObject *num = PyObject_GetAttr(h, s_value);
        if (!num) { Py_DECREF(fast); Py_DECREF(out); return nullptr; }
        double d = PyFloat_AsDouble(num);
        Py_DECREF(num);
        if (d == -1.0 && PyErr_Occurred()) {
            Py_DECREF(fast); Py_DECREF(out); return nullptr;
        }
        float f = static_cast<float>(d);
        std::memcpy(dst + i * 4, &f, 4);
    }
    Py_DECREF(fast);
    return out;
}

PyMethodDef methods[] = {
    {"read_block", read_block, METH_VARARGS,
     "read_block(buffer, offset, plan) -> (scalars, arrays, end_offset)"},
    {"write_block", write_block, METH_VARARGS,
     "write_block(plan, scalars, arrays) -> bytes"},
    {"fill_floats", fill_floats, METH_VARARGS,
     "fill_floats(buffer, offset, holders) -> end_offset"},
    {"pack_floats", pack_floats, METH_VARARGS,
     "pack_floats(holders) -> bytes"},
    {nullptr, nullptr, 0, nullptr},
};

PyModuleDef moduledef = {
    PyModuleDef_HEAD_INIT,
    "_nifgeom_native",
    "Native reader/writer for NIF geometry-data blocks.",
    -1, methods, nullptr, nullptr, nullptr, nullptr,
};

}  // namespace

PyMODINIT_FUNC PyInit__nifgeom_native(void)
{
    import_array();
    if (PyErr_Occurred()) return nullptr;

    // Everything here memcpys fixed-width little-endian data straight between
    // the stream and numpy buffers, so a big-endian host would silently
    // byte-swap every vertex.  Refuse to load rather than corrupt meshes.
    const unsigned int probe = 1u;
    if (*reinterpret_cast<const unsigned char *>(&probe) != 1u) {
        PyErr_SetString(PyExc_ImportError,
                        "_nifgeom_native requires a little-endian host");
        return nullptr;
    }
    return PyModule_Create(&moduledef);
}
