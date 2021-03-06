# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for JAX primitive coverage."""

import unittest

from absl.testing import absltest
from absl.testing import parameterized

from functools import partial

import jax
from jax import dtypes
from jax import lax
from jax import numpy as jnp
from jax import test_util as jtu
from jax.config import config
from jax.experimental import jax2tf
from jax.experimental.jax2tf.tests import tf_test_util
from jax.interpreters import xla

import numpy as np
import tensorflow as tf  # type: ignore[import]

config.parse_flags_with_absl()

# Import after parsing flags
from jax.experimental.jax2tf.tests import primitive_harness

REDUCE = (
  jnp.all,
  jnp.any,
  jnp.max,
  jnp.min,
  jnp.prod,
  jnp.sum,
)

INDEX = (
  jax.ops.index_add,
  jax.ops.index_max,
  jax.ops.index_min,
  jax.ops.index_mul,
  jax.ops.index_update,
)


class JaxPrimitiveTest(tf_test_util.JaxToTfTestCase):

  def test_primitive_coverage(self):
    """Fail if there are JAX primitives that are not implemented."""
    # Harvest primitives from XLA translation tables
    all_primitives = (set(xla.translations)
                      | set(xla.backend_specific_translations['cpu'])
                      | set(xla.backend_specific_translations['gpu'])
                      | set(xla.backend_specific_translations['tpu'])
                      | set(xla.initial_style_translations)
                      | set(xla.parallel_translations))

    tf_impl = set(jax.experimental.jax2tf.jax2tf.tf_impl)
    tf_not_yet_impl = set(jax.experimental.jax2tf.jax2tf.tf_not_yet_impl)

    all_primitives = tuple(sorted(all_primitives, key=str))
    for p in all_primitives:
      # TODO: remove tie_in once omnistaging is on by default
      if p.name == "axis_index" or p.name == "tie_in":
        continue
      if p in tf_not_yet_impl:
        self.assertNotIn(p, tf_impl)  # Should not be in both tf_impl and tf_not_yet_impl
      else:
        self.assertIn(p, tf_impl)

  @parameterized.named_parameters(jtu.cases_from_list(
    dict(testcase_name=f"_{f_jax.__name__}",
         f_jax=f_jax)
    for f_jax in [jnp.add, jnp.subtract, jnp.multiply, jnp.divide,
                  jnp.less, jnp.less_equal, jnp.equal, jnp.greater,
                  jnp.greater_equal, jnp.not_equal, jnp.maximum,
                  jnp.minimum]))
  def test_type_promotion(self, f_jax=jnp.add):
    # We only test a few types here, as tensorflow does not support many
    # types like uint* or bool in binary ops.
    types = [dtypes.bfloat16, np.int32, np.int64, np.float32]
    for x_dtype in types:
      for y_dtype in types:
        x = np.array([1, 2], dtype=x_dtype)
        y = np.array([3, 4], dtype=y_dtype)
        self.ConvertAndCompare(f_jax, x, y)

  def test_concat(self):
    values = [np.array([1, 2], dtype=np.float32),
              np.array([1, 2], dtype=np.int32),
              np.array([1, 2], dtype=np.int8)]
    f_jax = jax.jit(lambda x: jnp.concatenate(x, axis=0))
    self.ConvertAndCompare(f_jax, values)

  @primitive_harness.parameterized(primitive_harness.lax_pad)
  def test_pad(self, harness: primitive_harness.Harness):
    # TODO: fix pad with negative padding in XLA (fixed on 06/16/2020)
    if any([lo < 0 or hi < 0 for lo, hi, mid in harness.params["pads"]]):
      raise unittest.SkipTest("pad with negative pad not supported")
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_top_k)
  def test_top_k(self, harness: primitive_harness.Harness):
    if (harness.params["k"] > harness.params["shape"][-1] or
        harness.params["k"] < 0):
      with self.assertRaisesRegex(ValueError, "k argument to top_k must be"):
        harness.dyn_fun(*harness.dyn_args_maker(self.rng()))
    elif harness.params["dtype"] in jtu.dtypes.complex:
      # TODO(necula): fix top_k complex bug on TPU
      if jtu.device_under_test() == "tpu":
        raise unittest.SkipTest("top_k complex on TPU raises different error")
      with self.assertRaisesRegex(RuntimeError, "Unimplemented: complex comparison"):
        harness.dyn_fun(*harness.dyn_args_maker(self.rng()))
    # TODO: TF and JAX sort [inf, nan] differently.
    elif harness.name.startswith("nan_"):
      raise unittest.SkipTest("inconsistent [nan, inf] sorting")
    else:
      self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_sort)
  def test_sort(self, harness: primitive_harness.Harness):
    if harness.params["dtype"] in jtu.dtypes.complex:
      # TODO: implement complex support in XlaSort
      raise unittest.SkipTest("complex support not implemented")
    if harness.params["dtype"] is dtypes.bool_ and len(harness.arg_descriptors) == 4:
      # TODO: _sort uses tfxla.key_value_sort to handle 2 operandes, but the operation is not compatible with boolean keys.
      raise unittest.SkipTest("boolean key key value sort not implemented")
    if harness.params["is_stable"]:
      # TODO: implement stable sort support in XlaSort
      raise unittest.SkipTest("stable sort not implemented")
    if harness.params["dimension"] != len(harness.params["shape"]) - 1:
      # TODO: implement sort on all axes
      raise unittest.SkipTest("conversion not implemented for axis != -1")
    if len(harness.arg_descriptors) > 4:
      # TODO: implement variable number of operands to XlaSort
      raise unittest.SkipTest("conversion not implemented for #operands > 2")
    if (jtu.device_under_test() == "gpu" and
        len(harness.arg_descriptors) == 4 and
        not harness.params["is_stable"]):
      # TODO: fix the TF GPU test
      raise unittest.SkipTest("GPU tests are running TF on CPU")
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_fft)
  def test_fft(self, harness: primitive_harness.Harness):
    if len(harness.params["fft_lengths"]) > 3:
      with self.assertRaisesRegex(RuntimeError, "FFT only supports ranks 1-3"):
        harness.dyn_fun(*harness.dyn_args_maker(self.rng()))
    elif jtu.device_under_test() == "tpu" and len(harness.params["fft_lengths"]) > 1:
      # TODO(b/140351181): FFT is mostly unimplemented on TPU, even for JAX
      with self.assertRaisesRegex(RuntimeError, "only 1D FFT is currently supported."):
        harness.dyn_fun(*harness.dyn_args_maker(self.rng()))
    else:
      tol = None
      if jtu.device_under_test() == "gpu":
        if harness.params["dtype"] in jtu.dtypes.boolean:
          tol = 0.01
        else:
          tol = 1e-3
      self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()),
                             atol=tol, rtol=tol)

  @primitive_harness.parameterized(primitive_harness.lax_linalg_qr)
  def test_qr(self, harness: primitive_harness.Harness):
    # See jax.lib.lapack.geqrf for the list of compatible types

    dtype = harness.params["dtype"]
    dut = jtu.device_under_test()
    # These cases are not implemented in JAX
    if dtype in (jtu.dtypes.all_integer + [jnp.bfloat16]):
      unimplemented_jax = True
    elif dtype is np.complex64 and dut == "tpu":
      unimplemented_jax = True
    elif dtype is np.float16 and dut in ("cpu", "gpu"):
      unimplemented_jax = True
    else:
      unimplemented_jax = False

    if unimplemented_jax:
      raise unittest.SkipTest(f"QR not implemented in JAX for {dtype} on {dut}")

    expect_tf_exceptions = False
    if dtype in (np.complex64, np.complex128):
      expect_tf_exceptions = True
    # TODO: see https://github.com/google/jax/pull/3775#issuecomment-659407824.
    # - experimental_compile=True breaks for complex types;
    # - for now, the performance of the HLO QR implementation called when
    #   compiling with TF is expected to have worse performance than the
    #   custom calls made in JAX.
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()),
                           expect_tf_exceptions=expect_tf_exceptions,
                           atol=1e-5, rtol=1e-5)

  @primitive_harness.parameterized(primitive_harness.lax_linalg_svd)
  def test_svd(self, harness: primitive_harness.Harness):
    if jtu.device_under_test() == "tpu":
      raise unittest.SkipTest("TODO: test crashes the XLA compiler for some TPU variants")
    expect_tf_exceptions = False
    if harness.params["dtype"] in [np.float16, dtypes.bfloat16]:
      if jtu.device_under_test() == "tpu":
        # TODO: SVD on TPU for bfloat16 seems to work for JAX but fails for TF
        expect_tf_exceptions = True
      else:
        # Does not work in JAX
        with self.assertRaisesRegex(NotImplementedError, "Unsupported dtype"):
          harness.dyn_fun(*harness.dyn_args_maker(self.rng()))
        return

    if harness.params["dtype"] in [np.complex64, np.complex128]:
      if jtu.device_under_test() == "tpu":
        # TODO: on JAX on TPU there is no SVD implementation for complex
        with self.assertRaisesRegex(RuntimeError,
                                    "Binary op compare with different element types"):
          harness.dyn_fun(*harness.dyn_args_maker(self.rng()))
        return
      else:
        # TODO: on CPU and GPU "No registered 'Svd' OpKernel for XLA_CPU_JIT devices".
        # Works on JAX because JAX uses a custom implementation.
        expect_tf_exceptions = True

    def _custom_assert(r_jax, r_tf, atol=1e-6, rtol=1e-6):
      def _reconstruct_operand(result, is_tf: bool):
        # Reconstructing operand as documented in numpy.linalg.svd (see
        # https://numpy.org/doc/stable/reference/generated/numpy.linalg.svd.html)
        s, u, v = result
        if is_tf:
          s = s.numpy()
          u = u.numpy()
          v = v.numpy()
        U = u[..., :s.shape[-1]]
        V = v[..., :s.shape[-1], :]
        S = s[..., None, :]
        return jnp.matmul(U * S, V), s.shape, u.shape, v.shape

      if harness.params["compute_uv"]:
        r_jax_reconstructed = _reconstruct_operand(r_jax, False)
        r_tf_reconstructed = _reconstruct_operand(r_tf, True)
        self.assertAllClose(r_jax_reconstructed, r_tf_reconstructed,
                            atol=atol, rtol=rtol)
      else:
        self.assertAllClose(r_jax, r_tf, atol=atol, rtol=rtol)

    tol = 1e-4
    custom_assert = partial(_custom_assert, atol=tol, rtol=tol)

    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()),
                           atol=tol, rtol=tol,
                           expect_tf_exceptions=expect_tf_exceptions,
                           custom_assert=custom_assert,
                           always_custom_assert=True)

  @primitive_harness.parameterized(primitive_harness.lax_select_and_gather_add)
  def test_select_and_gather_add(self, harness: primitive_harness.Harness):
    dtype = harness.params["dtype"]

    max_bits = 64
    if jtu.device_under_test() == "tpu":
      max_bits = 32

    expect_tf_exceptions = False
    if dtypes.finfo(dtype).bits * 2 > max_bits:
      # TODO: getting an exception "XLA encountered an HLO for which this rewriting is not implemented"
      expect_tf_exceptions = True

    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()),
                           expect_tf_exceptions=expect_tf_exceptions)

  @primitive_harness.parameterized(primitive_harness.lax_reduce_window)
  def test_reduce_window(self, harness: primitive_harness.Harness):
    f_name = harness.params['computation'].__name__
    dtype = harness.params['dtype']

    expect_tf_exceptions = False

    if (jtu.device_under_test() == 'tpu' and dtype is np.complex64):
      raise unittest.SkipTest(
          'TODO: JAX reduce_window on TPU does not handle complex64'
      )

    if ((f_name == 'min' or f_name == 'max') and
        dtype not in [dtypes.bfloat16, np.float16, np.float32, np.float64, np.uint8,
                      np.int16, np.int32, np.int64]):
      # See https://www.tensorflow.org/api_docs/python/tf/math/minimum for a list of
      # the types supported by tf.math.minimum/tf.math.maximum.
      expect_tf_exceptions = True
    elif (f_name == 'add' and
          dtype not in [dtypes.bfloat16, np.float16, np.float32, np.float64, np.uint8,
                        np.int8, np.int16, np.int32, np.int64, np.complex64,
                        np.complex128]):
      # See https://www.tensorflow.org/api_docs/python/tf/math/add for a list of the
      # types supported by tf.math.add.
      expect_tf_exceptions = True
    elif (f_name == 'mul' and
          dtype not in [dtypes.bfloat16, np.float16, np.float32, np.float64, np.uint8,
                        np.int8, np.uint16, np.int16, np.int32, np.int64,
                        np.complex64, np.complex128]):
      # See https://www.tensorflow.org/api_docs/python/tf/math/multiply for a list of
      # the types supported by tf.math.multiply.
      expect_tf_exceptions = True

    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()),
                           expect_tf_exceptions=expect_tf_exceptions)

  @primitive_harness.parameterized(primitive_harness.lax_unary_elementwise)
  def test_unary_elementwise(self, harness: primitive_harness.Harness):
    dtype = harness.params["dtype"]
    lax_name = harness.params["lax_name"]
    if (lax_name in ("acosh", "asinh", "atanh", "bessel_i0e", "bessel_i1e", "digamma",
                     "erf", "erf_inv", "erfc", "lgamma", "round", "rsqrt") and
        dtype is dtypes.bfloat16 and
        jtu.device_under_test() in ["cpu", "gpu"]):
        raise unittest.SkipTest(f"bfloat16 support is missing from '{lax_name}' TF kernel on {jtu.device_under_test()} devices.")
    # TODO(bchetioui): do they have bfloat16 support, though?
    if lax_name in ("sinh", "cosh", "atanh", "asinh", "acosh", "erf_inv") and dtype is np.float16:
      raise unittest.SkipTest("b/158006398: float16 support is missing from '%s' TF kernel" % lax_name)
    arg, = harness.dyn_args_maker(self.rng())
    custom_assert = None
    if lax_name == "digamma":
      # TODO(necula): fix bug with digamma/(f32|f16) on TPU
      if dtype in [np.float16, np.float32] and jtu.device_under_test() == "tpu":
        raise unittest.SkipTest("TODO: fix bug: nan vs not-nan")

      # In the bfloat16 case, TF and lax both return NaN in undefined cases.
      if not dtype is dtypes.bfloat16:
        # digamma is not defined at 0 and -1
        def custom_assert(result_jax, result_tf):
          # lax.digamma returns NaN and tf.math.digamma returns inf
          special_cases = (arg == 0.) | (arg == -1.)
          nr_special_cases = np.count_nonzero(special_cases)
          self.assertAllClose(np.full((nr_special_cases,), dtype(np.nan)),
                              result_jax[special_cases])
          self.assertAllClose(np.full((nr_special_cases,), dtype(np.inf)),
                              result_tf[special_cases])
          # non-special cases are equal
          self.assertAllClose(result_jax[~ special_cases],
                              result_tf[~ special_cases])
    if lax_name == "erf_inv":
      # TODO(necula): fix erf_inv bug on TPU
      if jtu.device_under_test() == "tpu":
        raise unittest.SkipTest("erf_inv bug on TPU: nan vs non-nan")
      # TODO: investigate: in the (b)float16 cases, TF and lax both return the same
      # result in undefined cases.
      if not dtype in [np.float16, dtypes.bfloat16]:
        # erf_inv is not defined for arg <= -1 or arg >= 1
        def custom_assert(result_jax, result_tf):  # noqa: F811
          # for arg < -1 or arg > 1
          # lax.erf_inv returns NaN; tf.math.erf_inv return +/- inf
          special_cases = (arg < -1.) | (arg > 1.)
          nr_special_cases = np.count_nonzero(special_cases)
          self.assertAllClose(np.full((nr_special_cases,), dtype(np.nan)),
                              result_jax[special_cases])
          signs = np.where(arg[special_cases] < 0., -1., 1.)
          self.assertAllClose(np.full((nr_special_cases,), signs * dtype(np.inf)),
                              result_tf[special_cases])
          # non-special cases are equal
          self.assertAllClose(result_jax[~ special_cases],
                              result_tf[~ special_cases])
    atol = None
    if jtu.device_under_test() == "gpu":
      # TODO(necula): revisit once we fix the GPU tests
      atol = 1e-3
    self.ConvertAndCompare(harness.dyn_fun, arg, custom_assert=custom_assert,
                           atol=atol)

  @primitive_harness.parameterized(primitive_harness.lax_bitwise_not)
  def test_bitwise_not(self, harness):
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_population_count)
  def test_population_count(self, harness: primitive_harness.Harness):
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()),
                           expect_tf_exceptions=True)

  @primitive_harness.parameterized(primitive_harness.lax_add_mul)
  def test_add_mul(self, harness: primitive_harness.Harness):
    expect_tf_exceptions = False
    dtype = harness.params["dtype"]
    f_name = harness.params["f_jax"].__name__

    if dtype in [np.uint32, np.uint64]:
      # TODO(bchetioui): tf.math.multiply is not defined for the above types.
      expect_tf_exceptions = True
    elif dtype is np.uint16 and f_name == "add":
      # TODO(bchetioui): tf.math.add is defined for the same types as multiply,
      # except uint16.
      expect_tf_exceptions = True
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()),
                           expect_tf_exceptions=expect_tf_exceptions)

  @primitive_harness.parameterized(primitive_harness.lax_min_max)
  def test_min_max(self, harness: primitive_harness.Harness):
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_binary_elementwise)
  def test_binary_elementwise(self, harness):
    lax_name, dtype = harness.params["lax_name"], harness.params["dtype"]
    if lax_name in ("igamma", "igammac"):
      # TODO(necula): fix bug with igamma/f16
      if dtype in [np.float16, dtypes.bfloat16]:
        raise unittest.SkipTest("TODO: igamma(c) unsupported with (b)float16 in JAX")
      # TODO(necula): fix bug with igamma/f32 on TPU
      if dtype is np.float32 and jtu.device_under_test() == "tpu":
        raise unittest.SkipTest("TODO: fix bug: nan vs not-nan")
    arg1, arg2 = harness.dyn_args_maker(self.rng())
    custom_assert = None
    if lax_name == "igamma":
      # igamma is not defined when the first argument is <=0
      def custom_assert(result_jax, result_tf):
        # lax.igamma returns NaN when arg1 == arg2 == 0; tf.math.igamma returns 0
        special_cases = (arg1 == 0.) & (arg2 == 0.)
        nr_special_cases = np.count_nonzero(special_cases)
        self.assertAllClose(np.full((nr_special_cases,), np.nan),
                            result_jax[special_cases])
        self.assertAllClose(np.full((nr_special_cases,), 0.),
                            result_tf[special_cases])
        # non-special cases are equal
        self.assertAllClose(result_jax[~ special_cases],
                            result_tf[~ special_cases])
    if lax_name == "igammac":
      # igammac is not defined when the first argument is <=0
      def custom_assert(result_jax, result_tf):  # noqa: F811
        # lax.igammac returns 1. when arg1 <= 0; tf.math.igammac returns NaN
        special_cases = (arg1 <= 0.) | (arg2 <= 0)
        nr_special_cases = np.count_nonzero(special_cases)
        self.assertAllClose(np.full((nr_special_cases,), 1.),
                            result_jax[special_cases])
        self.assertAllClose(np.full((nr_special_cases,), np.nan),
                            result_tf[special_cases])
        # non-special cases are equal
        self.assertAllClose(result_jax[~ special_cases],
                            result_tf[~ special_cases])
    self.ConvertAndCompare(harness.dyn_fun, arg1, arg2,
                           custom_assert=custom_assert)

  @primitive_harness.parameterized(primitive_harness.lax_binary_elementwise_logical)
  def test_binary_elementwise_logical(self, harness):
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))


  @primitive_harness.parameterized(primitive_harness.lax_betainc)
  def test_betainc(self, harness: primitive_harness.Harness):
    # TODO: https://www.tensorflow.org/api_docs/python/tf/math/betainc only supports
    # float32/64 tests.
    # TODO(bchetioui): investigate why the test actually fails in JAX.
    if harness.params["dtype"] in [np.float16, dtypes.bfloat16]:
      raise unittest.SkipTest("(b)float16 not implemented in TF")
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  # TODO(necula): combine tests that are identical except for the harness
  # wait until we get more experience with using harnesses.
  @primitive_harness.parameterized(primitive_harness.lax_shift_left)
  def test_shift_left(self, harness):
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_shift_right_logical)
  def test_shift_right_logical(self, harness):
    if jtu.device_under_test() == "tpu" and harness.params["dtype"] in [np.int8, np.int16]:
      raise unittest.SkipTest("TODO: silent error for negative inputs")
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_shift_right_arithmetic)
  def test_shift_right_arithmetic(self, harness):
    if jtu.device_under_test() == "tpu" and harness.params["dtype"] in [np.uint8, np.uint16]:
      raise unittest.SkipTest("TODO: silent error for negative inputs")
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_slice)
  def test_slice(self, harness):
    # JAX.slice rejects negative indices; check, and skip jax2tf
    if any(si < 0 or si >= sh or li < 0 or li > sh
           for sh, si, li in zip(harness.params["shape"],
                                 harness.params["start_indices"],
                                 harness.params["limit_indices"])):
      with self.assertRaisesRegex(TypeError, ""):
        harness.dyn_fun(*harness.dyn_args_maker(self.rng()))
    else:
      self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_dynamic_slice)
  def test_dynamic_slice(self, harness):
    # JAX.dynamic_slice rejects slice sizes too big; check this, and skip jax2tf
    args = harness.dyn_args_maker(self.rng())
    expect_tf_exceptions = False
    if any(li - si < 0 or li - si >= sh
           for sh, si, li in zip(harness.params["shape"],
                                 harness.params["start_indices"],
                                 harness.params["limit_indices"])):
      with self.assertRaisesRegex(TypeError, ""):
        harness.dyn_fun(*args)
      return

    # TF sometimes gives errors for out-of-bounds accesses
    if any(si < 0 or li >= sh
          for sh, si, li in zip(harness.params["shape"],
                                harness.params["start_indices"],
                                harness.params["limit_indices"])):
      expect_tf_exceptions = True

    self.ConvertAndCompare(harness.dyn_fun, *args,
                           expect_tf_exceptions=expect_tf_exceptions)

  @primitive_harness.parameterized(primitive_harness.lax_dynamic_update_slice)
  def test_dynamic_update_slice(self, harness):
    # JAX.dynamic_update_slice rejects update slices too big; check, and skip jax2tf
    if any(ush > sh
           for sh, ush in zip(harness.params["shape"],
                              harness.params["update_shape"])):
      with self.assertRaisesRegex(TypeError, ""):
        harness.dyn_fun(*harness.dyn_args_maker(self.rng()))
    else:
      self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_squeeze)
  def test_squeeze(self, harness: primitive_harness.Harness):
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_gather)
  def test_gather(self, harness: primitive_harness.Harness):
    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()))

  @primitive_harness.parameterized(primitive_harness.lax_scatter)
  def test_scatter(self, harness: primitive_harness.Harness):
    f_name = harness.params['f_lax'].__name__
    dtype = harness.params['dtype']
    expect_tf_exceptions = False

    if jtu.device_under_test() == 'tpu':
      if dtype is np.complex64:
        if f_name in ['scatter_min', 'scatter_max']:
          raise unittest.SkipTest(f"TODO: complex {f_name} on TPU fails in JAX")
        else:
          # TODO: TensorFlow fails because of unimplemented cases
          expect_tf_exceptions = True

    if (f_name in ['scatter_min', 'scatter_max'] and
        dtype in [np.bool_, np.int8, np.uint16, np.uint32, np.uint64,
                  np.complex64, np.complex128]):
      # See https://www.tensorflow.org/api_docs/python/tf/math/minimum for a
      # list of the types supported by tf.math.minimum/tf.math.maximum.
      expect_tf_exceptions = True
    elif (f_name == 'scatter_add' and
          dtype in [np.uint16, np.uint32, np.uint64]):
      # See https://www.tensorflow.org/api_docs/python/tf/math/add for a list
      # of the types supported by tf.math.add.
      expect_tf_exceptions = True
    elif (f_name == 'scatter_mul' and dtype in [np.uint32, np.uint64]):
      # See https://www.tensorflow.org/api_docs/python/tf/math/multiply for a
      # list of the types supported by tf.math.multiply.
      expect_tf_exceptions = True

    self.ConvertAndCompare(harness.dyn_fun, *harness.dyn_args_maker(self.rng()),
                           expect_tf_exceptions=expect_tf_exceptions)

  def test_boolean_gather(self):
    values = np.array([[True, True], [False, True], [False, False]],
                      dtype=np.bool_)
    indices = np.array([0, 1], dtype=np.int32)
    for axis in [0, 1]:
      f_jax = jax.jit(lambda v, i: jnp.take(v, i, axis=axis))  # pylint: disable=cell-var-from-loop
      self.ConvertAndCompare(f_jax, values, indices)

  def test_gather_rank_change(self):
    params = jnp.array([[1.0, 1.5, 2.0], [2.0, 2.5, 3.0], [3.0, 3.5, 4.0]])
    indices = jnp.array([[1, 1, 2], [0, 1, 0]])
    f_jax = jax.jit(lambda i: params[i])
    self.ConvertAndCompare(f_jax, indices)

  @parameterized.named_parameters(jtu.cases_from_list(
    dict(testcase_name=f"_{f_jax.__name__}",
         f_jax=f_jax)
    for f_jax in REDUCE))
  def test_reduce_ops_with_numerical_input(self, f_jax):
    values = np.array([1, 2, 3], dtype=np.float32)
    self.ConvertAndCompare(f_jax, values)

  @parameterized.named_parameters(jtu.cases_from_list(
    dict(testcase_name=f"_{f_jax.__name__}",
         f_jax=f_jax)
    for f_jax in (jnp.cumsum, jnp.cumprod)))
  def test_cumulated_ops(self, f_jax):
    values = np.array([1, 2, 3], dtype=np.float32)
    self.ConvertAndCompare(f_jax, values)

  @parameterized.named_parameters(jtu.cases_from_list(
    dict(testcase_name=f"_{op.__name__}",
         op=op)
    for op in INDEX))
  def test_scatter_static(self, op):
    values = np.ones((5, 6), dtype=np.float32)
    update = np.float32(6.)
    f_jax = jax.jit(lambda v, u: op(v, jax.ops.index[::2, 3:], u))
    self.ConvertAndCompare(f_jax, values, update)

  @parameterized.named_parameters(jtu.cases_from_list(
    dict(testcase_name=f"_{f_jax.__name__}",
         f_jax=f_jax)
    for f_jax in REDUCE))
  def test_reduce_ops_with_boolean_input(self, f_jax):
    values = np.array([True, False, True], dtype=np.bool_)
    self.ConvertAndCompare(f_jax, values)

  def test_random_gamma(self):
    f_jax = jax.jit(jax.random.gamma)
    for alpha in [1.0,
                  np.array([1.0, 0.2, 1.2], np.float32),
                  np.array([1.0, 0.2, 1.2], np.float64)]:
      for rng_key in [jax.random.PRNGKey(42)]:
        self.ConvertAndCompare(f_jax, rng_key, alpha)

  def test_prngsplit(self):
    f_jax = jax.jit(lambda key: jax.random.split(key, 2))
    for rng_key in [jax.random.PRNGKey(42),
                    np.array([0, 0], dtype=np.uint32),
                    np.array([0xFFFFFFFF, 0], dtype=np.uint32),
                    np.array([0, 0xFFFFFFFF], dtype=np.uint32),
                    np.array([0xFFFFFFFF, 0xFFFFFFFF], dtype=np.uint32)
                    ]:
      self.ConvertAndCompare(f_jax, rng_key)

  def test_zeros_like(self):
    v = np.float32(2.)
    f_jax = jax.ad_util.zeros_like_jaxval
    self.ConvertAndCompare(f_jax, v)

  def test_stop_gradient(self):
    f = jax2tf.convert(lax.stop_gradient)
    self.assertEqual(f(tf.ones([])), 1.)

  # test_bfloat16_constant checks that https://github.com/google/jax/issues/3942 is
  # fixed
  def test_bfloat16_constant(self):
    def jax_fn_scalar(x):
      x = x.astype(jnp.bfloat16)
      x *= 2.
      return x

    def jax_fn_array(x):
      x = x.astype(jnp.bfloat16)
      x *= np.array([1.5, 2.5, 3.5], jnp.bfloat16)
      return x

    tf_fn_scalar = jax2tf.convert(jax_fn_scalar)
    self.assertAllClose(tf_fn_scalar(1.375).numpy(), jnp.bfloat16(2.750))

    tf_fn_array = jax2tf.convert(jax_fn_array)
    self.assertAllClose(tf_fn_array(np.array([3, 4, 5])),
                        np.array([4.5, 10, 17.5], jnp.bfloat16))

if __name__ == "__main__":
  absltest.main(testLoader=jtu.JaxTestLoader())
