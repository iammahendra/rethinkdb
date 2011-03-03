#include "btree/append_prepend.hpp"
#include "btree/modify_oper.hpp"
#include "buffer_cache/co_functions.hpp"

struct btree_append_prepend_oper_t : public btree_modify_oper_t {

    explicit btree_append_prepend_oper_t(data_provider_t *data, bool append)
        : data(data), append(append)
    { }

    bool operate(const boost::shared_ptr<transactor_t>& txor, btree_value *old_value, large_buf_lock_t& old_large_buflock, btree_value **new_value, large_buf_lock_t& new_large_buflock) {
        try {
            if (!old_value) {
                result = apr_not_found;
                return false;
            }

            size_t new_size = old_value->value_size() + data->get_size();
            if (new_size > MAX_VALUE_SIZE) {
                result = apr_too_large;
                return false;
            }

            // Copy flags, exptime, etc.

            valuecpy(&value, old_value);
            if (!old_value->is_large()) {
                // HACK: we set the value.size in advance preparation when
                // the old value was not large.  If the old value _IS_
                // large, we leave it with the old size, and then we
                // adjust value.size by refsize_adjustment below.

                // The vaule_size setter only behaves correctly when we're
                // setting a new large value.
                value.value_size(new_size, slice->cache().get_block_size());
            }


            // Figure out where the data is going to need to go and prepare a place for it

            if (new_size <= MAX_IN_NODE_VALUE_SIZE) { // small -> small
                rassert(!old_value->is_large());
                // XXX This does unnecessary copying now.
                if (append) {
                    buffer_group.add_buffer(data->get_size(), value.value() + old_value->value_size());
                } else { // prepend
                    memmove(value.value() + data->get_size(), value.value(), old_value->value_size());
                    buffer_group.add_buffer(data->get_size(), value.value());
                }
            } else {
                // Prepare the large value if necessary.
                if (!old_value->is_large()) { // small -> large; allocate a new large value and copy existing value into it.
                    large_buflock.set(new large_buf_t(txor->transaction()));
                    large_buflock.lv()->allocate(new_size, value.large_buf_ref_ptr(), btree_value::lbref_limit);
                    if (append) large_buflock.lv()->fill_at(0, old_value->value(), old_value->value_size());
                    else        large_buflock.lv()->fill_at(data->get_size(), old_value->value(), old_value->value_size());
                    is_old_large_value = false;
                } else { // large -> large; expand existing large value
                    memcpy(&value, old_value, MAX_BTREE_VALUE_SIZE);
                    large_buflock.swap(old_large_buflock);
                    large_buflock.lv()->HACK_root_ref(value.large_buf_ref_ptr());
                    int refsize_adjustment;

                    if (append) {
                        // TIED large_buflock TO value.
                        large_buflock.lv()->append(data->get_size(), &refsize_adjustment);
                    }
                    else {
                        large_buflock.lv()->prepend(data->get_size(), &refsize_adjustment);
                    }

                    value.size += refsize_adjustment;
                    is_old_large_value = true;
                }

                // Figure out the pointers and sizes where data needs to go

                uint32_t fill_size = data->get_size();
                uint32_t start_pos = append ? old_value->value_size() : 0;

                int64_t ix = large_buflock.lv()->pos_to_ix(start_pos);
                uint16_t seg_pos = large_buflock.lv()->pos_to_seg_pos(start_pos);

                while (fill_size > 0) {
                    uint16_t seg_len;
                    byte_t *seg = large_buflock.lv()->get_segment_write(ix, &seg_len);

                    rassert(seg_len >= seg_pos);
                    uint16_t seg_bytes_to_fill = std::min((uint32_t)(seg_len - seg_pos), fill_size);
                    buffer_group.add_buffer(seg_bytes_to_fill, seg + seg_pos);
                    fill_size -= seg_bytes_to_fill;
                    seg_pos = 0;
                    ix++;
                }
            }

            // Dispatch the data request

            result = apr_success;
            try {
                data->get_data_into_buffers(&buffer_group);
            } catch (data_provider_failed_exc_t) {
                if (large_buflock.has_lv()) {
                    if (is_old_large_value) {
                        // Some bufs in the large value will have been set dirty (and
                        // so new copies will be rewritten unmodified to disk), but
                        // that's not really a problem because it only happens on
                        // erroneous input.

                        int refsize_adjustment;
                        if (append) {
                            // TIED large_buflock TO value
                            large_buflock.lv()->unappend(data->get_size(), &refsize_adjustment);
                        }
                        else {
                            // TIED large_buflock TO value
                            large_buflock.lv()->unprepend(data->get_size(), &refsize_adjustment);
                        }
                        value.size += refsize_adjustment;
                    } else {
                        // The old value was small, so we just keep it and delete the large value
                        large_buf_lock_t empty;
                        large_buflock.lv()->mark_deleted();
                        large_buflock.swap(empty);
                    }
                }
                throw;
            }

            *new_value = &value;
            new_large_buflock.swap(large_buflock);
            return true;

        } catch (data_provider_failed_exc_t) {
            result = apr_data_provider_failed;
            return false;
        }
    }

    void actually_acquire_large_value(large_buf_t *lb, large_buf_ref *lbref) {
        if (append) {
            co_acquire_large_value_rhs(lb, lbref, btree_value::lbref_limit, rwi_write);
        } else {
            co_acquire_large_value_lhs(lb, lbref, btree_value::lbref_limit, rwi_write);
        }
    }

    append_prepend_result_t result;

    data_provider_t *data;
    bool append;   // true = append, false = prepend

    union {
        byte value_memory[MAX_BTREE_VALUE_SIZE];
        btree_value value;
    };
    bool is_old_large_value;
    large_buf_lock_t large_buflock;
    buffer_group_t buffer_group;
};

append_prepend_result_t btree_append_prepend(const store_key_t &key, btree_slice_t *slice, data_provider_t *data, bool append, castime_t castime) {
    btree_append_prepend_oper_t oper(data, append);
    run_btree_modify_oper(&oper, slice, key, castime);
    return oper.result;
}