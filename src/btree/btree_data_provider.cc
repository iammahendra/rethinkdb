#include "btree/btree_data_provider.hpp"
#include "buffer_cache/co_functions.hpp"

// Pulses the acquisition_cond once we no longer use the btree_value pointer.
small_value_data_provider_t::small_value_data_provider_t(const btree_value *_value, cond_t *acquisition_cond) : value(), buffers() {
    rassert(!_value->is_large());
    const byte *data = ptr_cast<byte>(_value->value());
    value.assign(data, data + _value->value_size());
    if (acquisition_cond) {
        acquisition_cond->pulse();
    }
}

size_t small_value_data_provider_t::get_size() const {
    return value.size();
}

const const_buffer_group_t *small_value_data_provider_t::get_data_as_buffers() throw (data_provider_failed_exc_t) {
    rassert(!buffers.get());

    buffers.reset(new const_buffer_group_t());
    buffers->add_buffer(get_size(), value.data());
    return buffers.get();
}

large_value_data_provider_t::large_value_data_provider_t(const btree_value *value, const boost::shared_ptr<transactor_t>& _transactor, cond_t *_acquisition_cond)
    : transactor(_transactor), buffers(), acquisition_cond(_acquisition_cond) {
    memcpy(&lb_ref, value->lb_ref(), value->lb_ref()->refsize((*transactor)->cache->get_block_size(), btree_value::lbref_limit));
}

size_t large_value_data_provider_t::get_size() const {
    return lb_ref.size;
}

const const_buffer_group_t *large_value_data_provider_t::get_data_as_buffers() throw (data_provider_failed_exc_t) {
    rassert(buffers.num_buffers() == 0);
    rassert(!large_value);

    thread_saver_t saver;
    large_value.reset(new large_buf_t(transactor, &lb_ref, btree_value::lbref_limit, rwi_read));
    co_acquire_large_buf(saver, large_value.get(), acquisition_cond);

    rassert(large_value->state == large_buf_t::loaded);

    large_value->bufs_at(0, lb_ref.size, true, &buffers);
    return const_view(&buffers);
}

value_data_provider_t *value_data_provider_t::create(const btree_value *value, const boost::shared_ptr<transactor_t>& transactor, cond_t *acquisition_cond) {
    if (value->is_large()) {
        return new large_value_data_provider_t(value, transactor, acquisition_cond);
    }
    else {
        return new small_value_data_provider_t(value, acquisition_cond);
    }
}

