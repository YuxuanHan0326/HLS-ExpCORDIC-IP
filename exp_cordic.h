#ifndef EXP_CORDIC_H_
#define EXP_CORDIC_H_

#include "ap_fixed.h"
#include "ap_int.h"

// ============================================================================
// Configurable Macros (sweep knobs)
// ============================================================================
#ifndef EXP_CORDIC_ITERS
#define EXP_CORDIC_ITERS 17
#endif

// Kept for compatibility with older code paths.
#ifndef EXP_CORDIC_ROM_LEN
#define EXP_CORDIC_ROM_LEN EXP_CORDIC_ITERS
#endif

#ifndef EXP_OUT_WL
#define EXP_OUT_WL 21
#endif
#ifndef EXP_OUT_IWL
#define EXP_OUT_IWL 1
#endif

#ifndef EXP_IN_WL
#define EXP_IN_WL 16
#endif
#ifndef EXP_IN_IWL
#define EXP_IN_IWL 5
#endif

#ifndef EXP_INT_WL
#define EXP_INT_WL 23
#endif
#ifndef EXP_INT_IWL
#define EXP_INT_IWL 4
#endif

// ============================================================================
// Type Aliases
// ============================================================================
typedef ap_fixed<EXP_IN_WL, EXP_IN_IWL> exp_in_t;
typedef ap_ufixed<EXP_OUT_WL, EXP_OUT_IWL> exp_out_t;
typedef ap_fixed<EXP_INT_WL, EXP_INT_IWL> exp_int_t;
typedef ap_int<12> exp_k_t;

// ============================================================================
// Top Function
// ============================================================================
exp_out_t exp_cordic_ip(exp_in_t x);

#endif  // EXP_CORDIC_H_
